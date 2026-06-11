from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.services import documentation
from src.services import state_snapshot
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, filter_items_for_user, get_current_user


router = APIRouter()


class LabDocumentationUpdateRequest(BaseModel):
    text: str = ""
    diagram: str = ""


class GeneralSectionUpdateRequest(BaseModel):
    title: str = ""
    text: str = ""


def _require_lab_visibility(user: AuthenticatedUser, vm_id: str, lab_name: str) -> None:
    if user.can_access_lab(vm_id, lab_name):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a ce LAB")


def _can_edit_documentation(user: AuthenticatedUser) -> bool:
    return user.role in {"admin", "group-admin"}


@router.get("/api/docs")
async def list_documentation(user: AuthenticatedUser = Depends(get_current_user)):
    items = await state_snapshot.get_or_refresh_snapshot_async()
    visible_items = filter_items_for_user(items, user)
    docs_map = documentation.list_docs_map()

    result = []
    for vm in visible_items:
        vm_id = str(vm.get("id") or "").strip()
        vm_name = str(vm.get("name") or vm_id)
        for lab in vm.get("labs") or []:
            lab_name = str(lab.get("name") or "").strip()
            if not vm_id or not lab_name:
                continue
            key = f"{vm_id}::{lab_name}"
            entry = docs_map.get(key) or documentation.get_doc(vm_id, lab_name)
            pdf_path = documentation.resolve_pdf_path(entry)
            result.append(
                {
                    "vm_id": vm_id,
                    "vm_name": vm_name,
                    "lab_name": lab_name,
                    "text": entry.get("text", ""),
                    "diagram": entry.get("diagram", ""),
                    "pdf_available": bool(pdf_path),
                    "pdf_name": entry.get("pdf_name", ""),
                    "updated_at": entry.get("updated_at", ""),
                    "updated_by": entry.get("updated_by", ""),
                }
            )

    result.sort(key=lambda item: (str(item.get("vm_name") or "").lower(), str(item.get("lab_name") or "").lower()))
    general_section = documentation.get_general_section()
    return {"ok": True, "items": result, "general_section": general_section}


@router.get("/api/docs/general")
async def get_general_section(user: AuthenticatedUser = Depends(get_current_user)):
    section = documentation.get_general_section()
    return {"ok": True, "section": section}


@router.put("/api/docs/general")
async def update_general_section(
    body: GeneralSectionUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Réservé aux administrateurs principaux")
    title = str(body.title or "").strip()
    text = str(body.text or "")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="Titre trop long (200 caractères max)")
    if len(text) > 500_000:
        raise HTTPException(status_code=400, detail="Texte trop long (500000 caractères max)")
    updated_at = datetime.now(timezone.utc).isoformat()
    section = documentation.upsert_general_section(
        title=title,
        text=text,
        updated_by=user.username,
        updated_at=updated_at,
    )
    record_event(
        request=request,
        user=user,
        action="doc.general_section.update",
        status="ok",
        details={"title": title, "text_len": len(text)},
    )
    return {"ok": True, "section": section}


@router.get("/api/docs/{vm_id}/labs/{lab_name}")
async def get_documentation_item(
    vm_id: str,
    lab_name: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_lab_visibility(user, vm_id, lab_name)
    entry = documentation.get_doc(vm_id, lab_name)
    pdf_path = documentation.resolve_pdf_path(entry)
    return {
        "ok": True,
        "item": {
            "vm_id": vm_id,
            "lab_name": lab_name,
            "text": entry.get("text", ""),
            "diagram": entry.get("diagram", ""),
            "pdf_available": bool(pdf_path),
            "pdf_name": entry.get("pdf_name", ""),
            "updated_at": entry.get("updated_at", ""),
            "updated_by": entry.get("updated_by", ""),
        },
    }


@router.put("/api/docs/{vm_id}/labs/{lab_name}")
async def update_documentation_item(
    vm_id: str,
    lab_name: str,
    body: LabDocumentationUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_lab_visibility(user, vm_id, lab_name)
    if not _can_edit_documentation(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Edition reservee aux administrateurs")

    text = str(body.text or "")
    diagram = str(body.diagram or "")
    if len(text) > 2_000_000:
        raise HTTPException(status_code=400, detail="Texte trop long (2000000 caracteres max)")
    if len(diagram) > 120000:
        raise HTTPException(status_code=400, detail="Diagramme trop long (120000 caracteres max)")

    updated_at = datetime.now(timezone.utc).isoformat()
    entry = documentation.upsert_text(
        vm_id=vm_id,
        lab_name=lab_name,
        text=text,
        diagram=diagram,
        updated_by=user.username,
        updated_at=updated_at,
    )

    record_event(
        request=request,
        user=user,
        action="lab.documentation.update",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"text_len": len(text), "diagram_len": len(diagram)},
    )

    return {
        "ok": True,
        "item": {
            "vm_id": vm_id,
            "lab_name": lab_name,
            "text": entry.get("text", ""),
            "diagram": entry.get("diagram", ""),
            "pdf_available": bool(documentation.resolve_pdf_path(entry)),
            "pdf_name": entry.get("pdf_name", ""),
            "updated_at": entry.get("updated_at", ""),
            "updated_by": entry.get("updated_by", ""),
        },
    }


@router.post("/api/docs/{vm_id}/labs/{lab_name}/pdf")
async def upload_documentation_pdf(
    vm_id: str,
    lab_name: str,
    request: Request,
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_lab_visibility(user, vm_id, lab_name)
    if not _can_edit_documentation(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Edition reservee aux administrateurs")

    filename = str(file.filename or "document.pdf")
    content_type = str(file.content_type or "").lower()
    if not filename.lower().endswith(".pdf") and content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Le fichier doit etre un PDF")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Fichier vide")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF trop volumineux (20MB max)")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Le fichier fourni n'est pas un PDF valide")

    updated_at = datetime.now(timezone.utc).isoformat()
    entry = documentation.save_pdf(
        vm_id=vm_id,
        lab_name=lab_name,
        original_name=filename,
        content=content,
        updated_by=user.username,
        updated_at=updated_at,
    )

    record_event(
        request=request,
        user=user,
        action="lab.documentation.pdf_upload",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"filename": entry.get("pdf_name", "")},
    )

    return {
        "ok": True,
        "item": {
            "vm_id": vm_id,
            "lab_name": lab_name,
            "pdf_available": bool(documentation.resolve_pdf_path(entry)),
            "pdf_name": entry.get("pdf_name", ""),
            "updated_at": entry.get("updated_at", ""),
            "updated_by": entry.get("updated_by", ""),
        },
    }


@router.delete("/api/docs/{vm_id}/labs/{lab_name}/pdf")
async def delete_documentation_pdf(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_lab_visibility(user, vm_id, lab_name)
    if not _can_edit_documentation(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Edition reservee aux administrateurs")

    updated_at = datetime.now(timezone.utc).isoformat()
    entry = documentation.remove_pdf(
        vm_id=vm_id,
        lab_name=lab_name,
        updated_by=user.username,
        updated_at=updated_at,
    )

    record_event(
        request=request,
        user=user,
        action="lab.documentation.pdf_delete",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
    )

    return {
        "ok": True,
        "item": {
            "vm_id": vm_id,
            "lab_name": lab_name,
            "pdf_available": bool(documentation.resolve_pdf_path(entry)),
            "pdf_name": entry.get("pdf_name", ""),
            "updated_at": entry.get("updated_at", ""),
            "updated_by": entry.get("updated_by", ""),
        },
    }


@router.get("/api/docs/{vm_id}/labs/{lab_name}/pdf")
async def get_documentation_pdf(
    vm_id: str,
    lab_name: str,
    download: bool = False,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_lab_visibility(user, vm_id, lab_name)
    entry = documentation.get_doc(vm_id, lab_name)
    pdf_path = documentation.resolve_pdf_path(entry)
    if not pdf_path:
        raise HTTPException(status_code=404, detail="Aucun PDF pour ce LAB")

    download_name = str(entry.get("pdf_name") or f"{lab_name}.pdf")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{download_name}"'},
    )