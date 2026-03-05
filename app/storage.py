from __future__ import annotations

import os
from fastapi import UploadFile

from app.config import settings

# Lazy-init S3 client
_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client(
            "s3",
            region_name=settings.spaces_region,
            endpoint_url=f"https://{settings.spaces_region}.digitaloceanspaces.com",
            aws_access_key_id=settings.spaces_access_key,
            aws_secret_access_key=settings.spaces_secret_key,
        )
    return _s3_client


def _spaces_enabled() -> bool:
    return bool(settings.spaces_bucket)


def _guess_content_type(ext: str) -> str:
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(ext.lower(), "application/octet-stream")


async def upload_image(file: UploadFile) -> str:
    """Upload image to Spaces or local filesystem. Returns the storage key/path."""
    filename = file.filename or "upload.jpg"
    _, ext = os.path.splitext(filename)
    ext = (ext or ".jpg").lower()
    name = f"{os.urandom(16).hex()}{ext}"

    content = await file.read()

    if _spaces_enabled():
        key = f"events/{name}"
        client = _get_s3_client()
        client.put_object(
            Bucket=settings.spaces_bucket,
            Key=key,
            Body=content,
            ACL="public-read",
            ContentType=_guess_content_type(ext),
        )
        return key
    else:
        # Local fallback
        os.makedirs(settings.upload_dir, exist_ok=True)
        out_path = os.path.join(settings.upload_dir, name)
        with open(out_path, "wb") as f:
            f.write(content)
        return out_path


def is_spaces_path(image_path: str) -> bool:
    """Detect if image_path is a Spaces key (events/...) vs local file."""
    return image_path.startswith("events/")


def get_image_url(image_path: str | None, event_id: int) -> str | None:
    """Return the URL for an event image. CDN URL for Spaces, proxy URL for local."""
    if not image_path:
        return None
    if is_spaces_path(image_path) and _spaces_enabled():
        domain = (
            settings.spaces_cdn_domain
            if settings.spaces_cdn_domain
            else f"{settings.spaces_bucket}.{settings.spaces_region}.digitaloceanspaces.com"
        )
        return f"https://{domain}/{image_path}"
    # Local: proxy through the backend endpoint
    return f"/api/v1/public/events/{event_id}/image"
