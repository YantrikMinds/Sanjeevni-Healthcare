from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any
import sys
import os
import json

# Ensure backend module can be imported
sys.path.append(os.getcwd())

from backend.utils.logger import app_logger, audit_logger
from backend.models.risk_engine import (
    RiskEngine, build_bilingual_plain_explanations,
    NOLAB_LABEL_EN, NOLAB_RECOMMENDATION_EN
)
from backend.models.cohort_engine import CohortEngine
from backend.models.counterfactuals import Counterfactuals
from backend.models.llm_engine import LLMEngine
from backend.services.history import HistoryService

# Import Schemas
from backend.schemas.patient import (
    PatientRequest, RiskResponse, ExplanationResponse, 
    ReportResponse, SimulationRequest, SimulationResponse,
    ReportGenerationRequest, CounterfactualResponse
)

# Import Routes
from backend.routes import auth, cohort, feedback, fhir, visit, audit
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.audit import record_audit_event
from backend.database import get_db
from sqlalchemy.orm import Session

from fastapi.staticfiles import StaticFiles
from backend.models.pdf_service import PDFService
from fastapi.responses import StreamingResponse

app = FastAPI(title="Sanjeevni Healthcarer API", version="1.0")


@app.on_event("startup")
def on_startup() -> None:
    """Create DB tables and seed demo users on first boot."""
    from backend.database import Base, engine, SessionLocal
    from backend.models import user as _user_mod, audit as _audit_mod, visit as _visit_mod, history as _hist_mod  # noqa: F401 – registers ORM models
    Base.metadata.create_all(bind=engine)

    # Auto-seed demo users when APP_ENV=development (safe – skips if already exist)
    if os.getenv("APP_ENV", "development").lower() == "development":
        try:
            import sys as _sys
            _sys.path.insert(0, os.getcwd())
            from scripts.seed_users import seed
            seed()
            app_logger.info("Demo users seeded successfully.")
        except Exception as _e:
            app_logger.warning(f"Demo user seed skipped (may already exist): {_e}")

# Mount PDF directory
pdf_dir = os.path.join(os.getcwd(), "backend", "pdfs")
os.makedirs(pdf_dir, exist_ok=True)
app.mount("/pdfs", StaticFiles(directory=pdf_dir), name="pdfs")

# 1. CORS Setup from Environment Variables
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
# If wildcard, use allow_origins=["*"] with allow_credentials=False
# If specific origins listed, allow_credentials=True (required for cookies/auth headers)
_wildcard = allowed_origins_env.strip() == "*"
allowed_origins = ["*"] if _wildcard else [o.strip() for o in allowed_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Singleton Engine Initialization & App State Attachment
try:
    risk_engine = RiskEngine()
    app.state.risk_engine = risk_engine
    app_logger.info("Risk Engine initialized successfully.")
except Exception as e:
    app_logger.error(f"Failed to load Risk Engine: {e}")
    risk_engine = None
    app.state.risk_engine = None

try:
    cohort_engine = CohortEngine()
    app.state.cohort_engine = cohort_engine
    app_logger.info("Cohort Engine initialized successfully.")
except Exception as e:
    app_logger.error(f"Failed to load Cohort Engine: {e}")
    cohort_engine = None
    app.state.cohort_engine = None

llm_engine = LLMEngine()
pdf_service = PDFService()

# 3. Include Routers
app.include_router(auth.router)
app.include_router(cohort.router)
app.include_router(feedback.router)
app.include_router(fhir.router)
app.include_router(visit.router)
app.include_router(audit.router)

def get_shared_risk_engine(request: Request) -> RiskEngine:
    engine = getattr(request.app.state, "risk_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Risk Engine not initialized")
    return engine

# --- Endpoints ---

@app.get("/health")
def health_check(request: Request):
    engine = getattr(request.app.state, "risk_engine", None)
    c_engine = getattr(request.app.state, "cohort_engine", None)
    return {
        "status": "healthy" if engine is not None else "degraded",
        "model_loaded": engine is not None,
        "cohort_engine_loaded": c_engine is not None,
        "environment": os.getenv("APP_ENV", "development")
    }

@app.get("/")
def root():
    return {
        "message": "Sanjeevni Healthcarer API is running", 
        "docs": "/docs",
        "role_guide": {
            "worker": "ASHA Community Health Worker screening",
            "doctor": "Primary Health Centre triage dashboard",
            "admin": "System audit and compliance logs"
        }
    }

@app.post("/predict", response_model=RiskResponse)
def predict_risk(
    patient: PatientRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        data = patient.model_dump()
        # Remove metadata before passing to ML model
        data.pop('clinician_id', None)
        data.pop('clinician_name', None)
        patient_name = data.pop('patient_name', None)
        village = data.pop('village', None)
        systolic = data.pop('systolic_bp', None)
        diastolic = data.pop('diastolic_bp', None)
        phq9 = data.pop('phq9_scores', None)

        no_lab = engine.is_no_lab(data)
        score = engine.predict_risk(data)
        level = get_risk_level(score)
        explanations = engine.explain_risk(data)
        
        # Save to JSON History
        try:
            HistoryService.save_record(
                patient_data=data, 
                risk_score=score, 
                risk_level=level
            )
        except Exception as hist_e:
            app_logger.warning(f"Failed to record patient history: {hist_e}")

        record_audit_event(
            db, current_user.email, current_user.role, "PREDICT", "/predict", "success",
            {"risk_level": level, "score": score, "lab_mode": "no_lab" if no_lab else "full_lab"}
        )

        return {
            "risk_score": score,
            "risk_level": level,
            "lab_mode": "no_lab" if no_lab else "full_lab",
            "model_mode_label": NOLAB_LABEL_EN if no_lab else None,
            "recommended_followup": NOLAB_RECOMMENDATION_EN if no_lab else None,
            "explanations": explanations,
        }
    except Exception as e:
        app_logger.exception("Prediction failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/history", response_model=Dict[str, Any])
def get_history(limit: int = 10, current_user: User = Depends(get_current_user)):
    return HistoryService.get_history(limit)

@app.post("/explain", response_model=ExplanationResponse)
def explain_risk(
    patient: PatientRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user)
):
    try:
        data = patient.model_dump()
        data.pop('clinician_id', None)
        data.pop('clinician_name', None)
        data.pop('patient_name', None)
        data.pop('village', None)
        data.pop('systolic_bp', None)
        data.pop('diastolic_bp', None)
        data.pop('phq9_scores', None)
        
        no_lab = engine.is_no_lab(data)
        explanations = engine.explain_risk(data)
        summary_en, summary_hi = build_bilingual_plain_explanations(explanations, data, no_lab=no_lab)
        return {
            "explanations": explanations,
            "summary_en": summary_en,
            "summary_hi": summary_hi
        }
    except Exception as e:
        app_logger.exception("Explain failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/counterfactuals", response_model=CounterfactualResponse)
def get_counterfactuals(
    patient: PatientRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user)
):
    try:
        cf = Counterfactuals(engine)
        p_dict = patient.model_dump()
        scenarios = cf.simulate_scenarios(p_dict)
        return {"scenarios": scenarios}
    except Exception as e:
        app_logger.exception("Counterfactual generation failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/model-card")
def get_model_card():
    from pathlib import Path
    metrics_path = Path("backend/models/metrics.json")
    fairness_path = Path("backend/models/fairness.json")
    metrics_data = {}
    fairness_data = {}
    if metrics_path.exists():
        with open(metrics_path, "r", encoding="utf-8") as f:
            metrics_data = json.load(f)
    if fairness_path.exists():
        with open(fairness_path, "r", encoding="utf-8") as f:
            fairness_data = json.load(f)
            
    return {
        "model_name": "Sanjeevni Calibrated XGBoost Risk Engine (v1.0)",
        "intended_use": "Rural community health worker screening and clinical decision support for 5-minute visits. Triages patients into Green, Yellow, and Red pathways.",
        "data_source": "Public Kaggle diabetes benchmark dataset (100,992 records) with demographics, BMI, hypertension, heart disease, smoking, glucose, and HbA1c.",
        "limitations": [
            "Research prototype for decision support only.",
            "Not an automated diagnostic tool or clinical device.",
            "Requires confirmatory venous blood testing and clinical evaluation by a licensed physician.",
            "Trained on the public Kaggle diabetes benchmark dataset; subgroup recall is lower for adults under 45 and false-positive rate is higher for adults 60+.",
            fairness_data.get("age_audit", {}).get("limitation_note", "")
        ],
        "metrics": metrics_data,
        "fairness": fairness_data,
        "disclaimer": "Sanjeevni Healthcarer is an explainable AI research prototype for clinical decision support. It is not an FDA/CDSCO approved diagnostic device and must never replace qualified clinical judgment."
    }

@app.post("/simulate", response_model=SimulationResponse)
def simulate_risk(
    request: SimulationRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user)
):
    try:
        cf = Counterfactuals(engine)
        p_dict = request.patient.model_dump()
        result = cf.predict_simulation(p_dict, request.modifications)
        
        original_risk = engine.predict_risk(p_dict)
        new_risk = result['new_risk']
        
        return {
            "original_risk": original_risk,
            "new_risk": new_risk,
            "risk_reduction": original_risk - new_risk
        }
    except Exception as e:
        app_logger.exception("Simulation failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/simulate/report", response_model=ReportResponse)
def generate_simulation_report(
    request: SimulationRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user)
):
    try:
        p_dict = request.patient.model_dump()
        original_risk = engine.predict_risk(p_dict)
        
        cf = Counterfactuals(engine)
        result = cf.predict_simulation(p_dict, request.modifications)
        new_risk = result['new_risk']
        
        # Safe verified clinical summary
        report = llm_engine.generate_doctor_summary(
            patient_data=request.patient.model_dump(),
            ml_risk_score=new_risk,
            final_triage="SIMULATED",
            escalation_reasons=[f"Risk reduced by {((original_risk - new_risk)*100):.1f}%"]
        )
        return {"report": report}
    except Exception as e:
        app_logger.exception("Simulation report generation failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/report", response_model=ReportResponse)
def generate_report(
    patient: PatientRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        data = patient.model_dump()
        data.pop('clinician_id', None)
        data.pop('clinician_name', None)
        patient_name = data.pop('patient_name', None)
        data.pop('village', None)
        data.pop('systolic_bp', None)
        data.pop('diastolic_bp', None)
        data.pop('phq9_scores', None)

        score = engine.predict_risk(data)
        level = get_risk_level(score)
        explanations = engine.explain_risk(data)
        
        if llm_engine is None or not llm_engine.is_available():
            app_logger.info("Using standard template report (LLM offline).")
            report = f"""
            **Clinical Validation Summary:**
            Based on the assessment, the patient is classified as **{level.upper()} RISK** (Score: {score:.2f}). 
            
            **Key Drivers:**
            The primary contributing factors appear to be {explanations[0]['feature'] if explanations else 'metabolic indicators'}.
            
            **Actionable Community Care Advice:**
            1. Initiation of standard lifestyle interventions is recommended.
            2. Follow-up testing for HbA1c and vitals monitoring.
            3. Consider clinical review at Primary Health Centre if risk factors persist.
            
            *Research prototype disclaimer: For clinical screening assistance only; not a diagnostic replacement.*
            """
        else:
            report = llm_engine.generate_doctor_summary(data, score, level.upper(), [e.get("feature", "") for e in explanations])
        
        pdf_filename = None
        pdf_url = None
        
        if pdf_service:
            try:
                pdf_filename = pdf_service.generate_report(data, score, level, report, explanations, patient_name=patient_name)
                pdf_url = f"/pdfs/{pdf_filename}"
            except Exception as pdf_e:
                app_logger.error(f"Error generating PDF: {pdf_e}")

        record_audit_event(db, current_user.email, current_user.role, "REPORT", "/report", "success", {"risk_level": level})
        return {"report": report.strip(), "pdf_url": pdf_url}
    except Exception as e:
        app_logger.exception("Report generation failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/llm/health")
def llm_health_check():
    """Health check for external LLM endpoint."""
    online = llm_engine.is_available()
    return {
        "status": "online" if online else "offline",
        "model": llm_engine.model,
        "base_url": llm_engine.base_url,
        "mock_mode": llm_engine.mock_mode
    }

@app.post("/chat")
def chat_endpoint(
    request: Dict[str, Any],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Standard grounded chat endpoint."""
    msg = request.get("message", "")
    history = request.get("history", [])
    record_audit_event(db, current_user.email, current_user.role, "CHAT", "/chat", "success")
    chunks = list(llm_engine.stream_chat(msg, history))
    return {"reply": "".join(chunks)}

@app.post("/chat/stream")
def chat_stream_endpoint(
    request: Dict[str, Any],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Server-Sent Events streaming chatbot endpoint."""
    msg = request.get("message", "")
    history = request.get("history", [])
    record_audit_event(db, current_user.email, current_user.role, "CHAT", "/chat/stream", "success")

    def event_generator():
        for chunk in llm_engine.stream_chat(msg, history):
            yield f"data: {json.dumps({'text': chunk, 'chunk': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/report/stream")
def generate_report_stream(
    patient: ReportGenerationRequest,
    engine: RiskEngine = Depends(get_shared_risk_engine),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    record_audit_event(db, current_user.email, current_user.role, "REPORT", "/report/stream", "success")
    data = patient.model_dump()
    # Extract optional pre-calculated data
    pre_score = data.pop('risk_score', None)
    pre_level = data.pop('risk_level', None)
    pre_explanations = data.pop('explanations', None)

    # Remove metadata
    data.pop('clinician_id', None)
    data.pop('clinician_name', None)
    patient_name = data.pop('patient_name', None)
    data.pop('village', None)
    data.pop('systolic_bp', None)
    data.pop('diastolic_bp', None)
    data.pop('phq9_scores', None)

    # Use pre-calculated data if available, otherwise calculate
    if pre_score is not None and pre_explanations is not None:
        score = pre_score
        level = pre_level if pre_level else get_risk_level(score)
        explanations = pre_explanations
    else:
        score = engine.predict_risk(data)
        level = get_risk_level(score)
        explanations = engine.explain_risk(data)

    def event_generator():
        # 1. Yield Risk Data first (fast)
        yield f"event: risk\ndata: {json.dumps({'risk_score': score, 'risk_level': level})}\n\n"
        
        full_report = ""
        
        # 2. Yield LLM tokens or verified template
        if llm_engine is None:
            mock_text = f"Clinical Screening Summary: Patient is {level} Risk with score {score:.2f}. Advise lifestyle monitoring and standard primary care follow-up."
            for word in mock_text.split():
                yield f"data: {word} \n\n"
                full_report += word + " "
        else:
            summary = llm_engine.generate_doctor_summary(data, score, level.upper(), [e.get("feature", "") for e in explanations])
            for word in summary.split():
                yield f"data: {word} \n\n"
                full_report += word + " "
        
        # 3. Generate PDF in background
        pdf_url = None
        if pdf_service:
            try:
                pdf_filename = pdf_service.generate_report(data, score, level, full_report, explanations, patient_name=patient_name)
                pdf_url = f"/pdfs/{pdf_filename}"
            except Exception as e:
                app_logger.error(f"Error generating PDF: {e}")
        
        # 4. Yield Final PDF URL
        if pdf_url:
            yield f"event: pdf\ndata: {json.dumps({'pdf_url': pdf_url})}\n\n"
            
        yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def get_risk_level(score):
    if score < 0.3: return "Low"
    if score < 0.7: return "Moderate"
    return "High"


# ── Mental Health Screening Endpoint ──────────────────────────────────────────
from pydantic import BaseModel
from typing import List as TypingList

class PHQ9Request(BaseModel):
    scores: TypingList[int]  # 9 scores, each 0-3

class PHQ9Response(BaseModel):
    total_score: int
    severity: str
    advice: str
    crisis_recommended: bool

@app.post("/mental-health-screen", response_model=PHQ9Response)
def mental_health_screen(request: PHQ9Request, current_user: User = Depends(get_current_user)):
    """PHQ-9 Mental Health Screening — pure Python, no ML needed."""
    if len(request.scores) != 9:
        raise HTTPException(status_code=400, detail="Exactly 9 scores required (0-3 each)")
    for s in request.scores:
        if s not in [0, 1, 2, 3]:
            raise HTTPException(status_code=400, detail="Each score must be 0, 1, 2, or 3")

    total = sum(request.scores)
    crisis = request.scores[8] > 0  # Q9 — self-harm thoughts

    if total <= 4:
        severity, advice = "Minimal", "Minimal depressive symptoms. Maintain healthy habits and self-care."
    elif total <= 9:
        severity, advice = "Mild", "Mild symptoms. Consider speaking to a counsellor for guidance."
    elif total <= 14:
        severity, advice = "Moderate", "Moderate depression. We recommend consulting a mental health professional."
    elif total <= 19:
        severity, advice = "Moderately Severe", "Please reach out to a mental health professional or crisis helpline immediately."
    else:
        severity, advice = "Severe", "URGENT: Severe depression. Contact crisis helpline or emergency services now."

    return PHQ9Response(total_score=total, severity=severity, advice=advice, crisis_recommended=crisis)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
