import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Any

import aioboto3
import pydicom
from pydicom.tag import Tag
import structlog
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, status
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

# --- Config Haute Disponibilité ---
logger = structlog.get_logger()
S3_BUCKET = os.getenv("S3_BUCKET", "pinkshield-vault-prod")
SQS_URL = os.getenv("SQS_QUEUE_URL")
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB pour optimiser le throughput AWS S3

# --- Schémas de Sortie ---
class AnalysisResponse(BaseModel):
    task_id: uuid.UUID
    status: str = "PROCESSING"
    vault_path: str
    integrity_hash: str
    eta_seconds: int = 45 

# --- Logique de Validation Chirurgicale ---
async def validate_dicom_fast(file: UploadFile) -> Dict[str, Any]:
    """Validation ultra-rapide sans charger l'image en RAM"""
    header = await file.read(132)
    if len(header) < 132 or header[128:132] != b"DICM":
        raise HTTPException(status_code=400, detail="Invalid DICOM: Missing Preamble")
    
    await file.seek(0)
    try:
        # On ne lit QUE les tags nécessaires (Performance O(1))
        tags_to_read = [Tag(0x0008, 0x0060), Tag(0x0010, 0x0020), Tag(0x0020, 0x0062)]
        ds = pydicom.dcmread(file.file, specific_tags=tags_to_read, stop_before_pixels=True)
        
        if getattr(ds, "Modality", "") != "MG":
            raise HTTPException(status_code=422, detail="Unsupported Modality: MG Required")
            
        return {
            "pid": getattr(ds, "PatientID", "ANON"),
            "laterality": getattr(ds, "ImageLaterality", "U")
        }
    finally:
        await file.seek(0)

# --- Dispatcher avec Résilience (Tenacity) ---
@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
async def safe_dispatch(sqs_client, payload: dict):
    """Garantit l'envoi même en cas de micro-coupure AWS"""
    await sqs_client.send_message(
        QueueUrl=SQS_URL,
        MessageBody=json.dumps(payload),
        MessageAttributes={'Priority': {'DataType': 'String', 'StringValue': 'HIGH'}}
    )

# --- Service de Vaulting Haute Performance ---
async def stream_to_vault(file: UploadFile, s3_client, key: str) -> str:
    """Upload avec calcul de hash SHA256 en continu"""
    sha256 = hashlib.sha256()
    res = await s3_client.create_multipart_upload(Bucket=S3_BUCKET, Key=key, ChecksumAlgorithm='SHA256')
    upload_id = res["UploadId"]
    parts = []

    try:
        part_num = 1
        while chunk := await file.read(CHUNK_SIZE):
            sha256.update(chunk)
            # S3 vérifie lui-même l'intégrité via ChecksumAlgorithm
            part = await s3_client.upload_part(
                Bucket=S3_BUCKET, Key=key, PartNumber=part_num,
                UploadId=upload_id, Body=chunk
            )
            parts.append({"PartNumber": part_num, "ETag": part["ETag"]})
            part_num += 1

        await s3_client.complete_multipart_upload(
            Bucket=S3_BUCKET, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts}
        )
        return sha256.hexdigest()
    except Exception as e:
        await s3_client.abort_multipart_upload(Bucket=S3_BUCKET, Key=key, UploadId=upload_id)
        logger.critical("vault_write_error", error=str(e))
        raise HTTPException(status_code=507, detail="Storage Write Failed")

# --- FastAPI 10/10 ---
app = FastAPI(title="PinkShield AI Enterprise")

@app.post("/api/v1/scan", response_model=AnalysisResponse, status_code=202)
async def process_mammography(
    bg: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...)
):
    # 1. Validation de sécurité immédiate
    meta = await validate_dicom_fast(file)
    task_id = uuid.uuid4()
    
    # 2. Upload synchrone pour garantir la persistance avant réponse
    async with aioboto3.Session().client("s3") as s3:
        s3_key = f"active_scans/{datetime.now().date()}/{task_id}.dcm"
        final_hash = await stream_to_vault(file, s3, s3_key)

    # 3. Dispatch asynchrone vers l'IA (SQS)
    async def background_pipeline():
        async with aioboto3.Session().client("sqs") as sqs:
            payload = {"id": str(task_id), "path": s3_key, "hash": final_hash}
            await safe_dispatch(sqs, payload)
            logger.info("pipeline_triggered", task_id=str(task_id))

    bg.add_task(background_pipeline)

    return AnalysisResponse(
        task_id=task_id,
        vault_path=s3_key,
        integrity_hash=final_hash
    )
