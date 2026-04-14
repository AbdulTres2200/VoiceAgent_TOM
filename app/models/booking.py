from pydantic import BaseModel


class BookingRequest(BaseModel):
    customer_name: str
    guest_count: int
    appointment_time: str


class BookingResponse(BaseModel):
    success: bool
    message: str
    booking_details: dict
