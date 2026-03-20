import torch
import torch.nn.functional as F
import numpy as np
import cv2
import uvicorn
import base64
import io
import time
import asyncio
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from loguru import logger
from typing import List, Optional
from PIL import Image
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 1. CONFIGURATION INDUSTRIELLE
# ==========================================
class Settings:
    PROJECT_NAME: str = "PinkShield Ultra-SaaS Engine"
    VERSION: str = "13.0.0"
    DEVICE: torch.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    MAX_IMAGE_SIZE: int = 10 * 1024 * 1024
    ALLOWED_FORMATS: set = {"image/jpeg", "image/png", "image/webp"}
    # On limite le nombre de threads pour le CPU bound (OpenCV/PIL)
    MAX_WORKERS: int = 4 

settings = Settings()

class AnalysisResponse(BaseModel):
    status: str
    label: str
    confidence: float
    heatmap_base64: str
    latency_ms: float

# ==========================================
# 2. MOTEUR D'INFÉRENCE SÉNIOR
# ==========================================
class AIInternalEngine:
    def __init__(self):
        from torchvision.models import swin_b, Swin_B_Weights
        self.weights = Swin_B_Weights.DEFAULT
        self.model = swin_b(weights=self.weights).to(settings.DEVICE)
        
        if hasattr(torch, 'compile') and settings.DEVICE.type == 'cuda':
            # Mode "reduce-overhead" pour une latence minimale en SaaS
            self.model = torch.compile(self.model, mode="reduce-overhead")
            
        self.model.eval()
        self.preprocess = self.weights.transforms()
        self.categories = self.weights.meta["categories"]
        logger.success(f"🚀 Engine V13.0 Active | Device: {settings.DEVICE}")

    def _process_heatmap(self, grad, act, size):
        """Calcul Grad-CAM optimisé pour l'architecture Swin (Non-CNN)"""
        # Swin renvoie souvent (B, L, C) -> On reshape en (B, C, H, W)
        b, l, c = act.shape
        grid = int(np.sqrt(l))
        grad = grad.reshape(b, grid, grid, c).permute(0, 3, 1, 2)
        act = act.reshape(b, grid, grid, c).permute(0, 3, 1, 2)
        
        weights = torch.mean(grad, dim=(2, 3), keepdim=True)
        cam = F.relu(torch.sum(weights * act, dim=1)).squeeze().detach().cpu().numpy()
        
        # Normalisation robuste
        cam_min, cam_max = cam.min(), cam.max()
        cam = (cam - cam_min) / (cam_max - cam_min + 1e-7)
        return cv2.resize(cam, size)

    def predict_sync(self, image_bytes: bytes) -> AnalysisResponse:
        """Méthode synchrone pour exécution en ThreadPool (évite de bloquer l'Event Loop)"""
        start_time = time.perf_counter()
        
        try:
            img_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            orig_sz = img_pil.size
        except Exception:
            raise ValueError("Invalid image format")

        # Préparation du tenseur
        input_tensor = self.preprocess(img_pil).unsqueeze(0).to(settings.DEVICE)
        input_tensor.requires_grad = True

        activations, gradients = [], []
        # Ciblage du dernier bloc de shift-window attention pour la pertinence spatiale
        target_layer = self.model.layers[-1].blocks[-1].norm1
        
        def save_grad(grad): gradients.append(grad)
        def hook_fn(m, i, o):
            activations.append(o)
            o.register_hook(save_grad)

        handle = target_layer.register_forward_hook(hook_fn)

        try:
            with torch.cuda.amp.autocast(enabled=(settings.DEVICE.type == 'cuda')):
                output = self.model(input_tensor)
                probs = torch.softmax(output, dim=1)
                conf, idx = torch.max(probs, dim=1)

            self.model.zero_grad()
            conf.backward()

            # --- Génération Visuelle ---
            heatmap = self._process_heatmap(gradients[0], activations[0], orig_sz)
            img_cv = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
            heatmap_color = cv2.applyColorMap(np.uint8(255 * heatmap), cv2.COLORMAP_JET)
            overlay = cv2.addWeighted(img_cv, 0.6, heatmap_color, 0.4, 0)
            
            _, buffer = cv2.imencode('.jpg', overlay, [cv2.IMWRITE_JPEG_QUALITY, 85])
            base64_img = base64.b64encode(buffer).decode('utf-8')

        finally:
            handle.remove()
            # Nettoyage mémoire explicite (Critique pour le multi-threading)
            del input_tensor, output, activations, gradients
            if settings.DEVICE.type == 'cuda':
                torch.cuda.empty_cache()

        return AnalysisResponse(
            status="success",
            label=self.categories[idx.item()],
            confidence=round(float(conf.item()), 4),
            heatmap_base64=base64_img,
            latency_ms=round((time.perf_counter() - start_time) * 1000, 2)
        )

# ==========================================
# 3. ROUTAGE & LIFECYCLE
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = AIInternalEngine()
    app.state.executor = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS)
    yield
    app.state.executor.shutdown()

app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

@app.post("/v1/analyze", response_model=AnalysisResponse)
async def analyze(file: UploadFile = File(...)):
    if file.content_type not in settings.ALLOWED_FORMATS:
        raise HTTPException(415, "Format non supporté")

    content = await file.read()
    
    # Exécution asynchrone hors de l'event loop principale
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            app.state.executor, 
            app.state.engine.predict_sync, 
            content
        )
    except Exception as e:
        logger.error(f"Inference Error: {e}")
        raise HTTPException(500, "Erreur interne du moteur d'IA")

@app.get("/health")
async def health():
    return {"status": "healthy", "workers": settings.MAX_WORKERS}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
