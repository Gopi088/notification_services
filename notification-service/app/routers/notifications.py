import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.database import create_message, get_message
from app.schemas import ErrorResponse, SendRequest, SendResponse, StatusResponse
from app.services.notification_service import process_message
from app.validation import ContactValidationError, validate_contact

router = APIRouter()


@router.post(
    "/send",
    response_model=SendResponse,
    responses={400: {"model": ErrorResponse}},
    status_code=202,
    summary="Queue a message for delivery over WhatsApp, SMS, or Email",
)
def send_message(payload: SendRequest, background_tasks: BackgroundTasks) -> SendResponse:
    try:
        validate_contact(payload.channel, payload.contact)
    except ContactValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    message_id = str(uuid.uuid4())
    create_message(
        message_id=message_id,
        channel=payload.channel.value,
        contact=payload.contact,
        message=payload.message,
        status="queued",
    )

    # Sending happens after the response is returned so /send responds
    # immediately; poll GET /status/{message_id} for the outcome.
    background_tasks.add_task(process_message, message_id, payload.channel, payload.contact, payload.message)

    return SendResponse(message_id=message_id, status="queued")


@router.get(
    "/status/{message_id}",
    response_model=StatusResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Get the current delivery status of a previously sent message",
)
def get_status(message_id: str) -> StatusResponse:
    row = get_message(message_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No message found with id '{message_id}'")

    return StatusResponse(
        message_id=row["message_id"],
        channel=row["channel"],
        contact=row["contact"],
        status=row["status"],
        provider=row["provider"],
        provider_message_id=row["provider_message_id"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
