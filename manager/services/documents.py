import mimetypes
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from manager.models import AppSetting


ALLOWED_MIME = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
OFFICE_MIME = {k for k, v in ALLOWED_MIME.items() if v in {"docx", "xlsx", "pptx"}}


def inspect_document(path):
    result = subprocess.run(["file", "--brief", "--mime-type", str(path)], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError("The uploaded file could not be inspected")
    mime = result.stdout.strip()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise ValueError("Macro-bearing Office documents are not accepted")
            if "word/document.xml" in names:
                mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif "xl/workbook.xml" in names:
                mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            elif "ppt/presentation.xml" in names:
                mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if mime not in ALLOWED_MIME:
        raise ValueError("Only PDF, PNG, JPEG, DOCX, XLSX, and PPTX files are accepted")
    return mime


def _validate_pdf(path):
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs cannot be printed")
    if len(reader.pages) > 1000:
        raise ValueError("Documents are limited to 1,000 pages")


def normalize_for_print(source, mime, destination_dir):
    source = Path(source)
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = destination_dir / "print.pdf"
    if mime == "application/pdf":
        _validate_pdf(source)
        shutil.copyfile(source, output)
    elif mime in {"image/png", "image/jpeg"}:
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            image.convert("RGB").save(output, "PDF", resolution=150.0)
    elif mime in OFFICE_MIME:
        profile = tempfile.mkdtemp(prefix="lo-profile-", dir="/tmp")
        try:
            conversion_timeout = AppSetting.get_int("office_conversion_timeout_seconds", 120, minimum=30, maximum=600)
            command = [
                "timeout", str(conversion_timeout), "libreoffice", "--headless", "--nologo", "--nodefault", "--nolockcheck",
                f"-env:UserInstallation=file://{profile}", "--convert-to", "pdf", "--outdir", str(destination_dir), str(source),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=conversion_timeout + 10, env={**os.environ, "HOME": profile})
            converted = destination_dir / f"{source.stem}.pdf"
            if result.returncode or not converted.exists():
                raise ValueError("Office document conversion failed")
            converted.replace(output)
            _validate_pdf(output)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    else:
        raise ValueError("Unsupported document type")
    os.chmod(output, 0o600)
    return output
