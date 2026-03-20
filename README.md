PinkShield Ultra-SaaS Vault API




🚀 Description

PinkShield Ultra-SaaS Vault API is a high-performance SaaS engine for secure management and processing of DICOM files. It provides asynchronous uploads, resilient storage to AWS S3, and dispatch to SQS for integration with downstream analysis pipelines.

Optimized for SaaS environments, it minimizes memory usage and handles large file uploads efficiently using streaming.

🔹 Key Features

Fast DICOM validation without loading the full image into memory.

Modality check (MG for mammography) and essential DICOM tag extraction.

Streaming multipart upload to S3 with continuous SHA256 hash calculation.

Asynchronous dispatch to SQS with retries and resilience (Tenacity).

FastAPI background tasks for non-blocking pipelines.

Optimized for CPU and cloud SaaS environments (low memory, high throughput).

Structured logging with structlog for traceability.
