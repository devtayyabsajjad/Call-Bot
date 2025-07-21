import os
import logging
from datetime import datetime, timezone
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Voice Chatbot Appointment Booking", version="1.0.0")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-react-app.com", "*"],  # Update with your React app URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
RECEPTION_WHATSAPP = os.getenv("RECEPTION_WHATSAPP")
FALLBACK_NUMBER = os.getenv("FALLBACK_NUMBER")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialize clients
try:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Successfully initialized Twilio and Supabase clients")
except Exception as e:
    logger.error(f"Failed to initialize clients: {e}")
    raise

# Pydantic models
class BookSlotRequest(BaseModel):
    slot_id: int

class AppointmentSlot(BaseModel):
    id: int
    slot_time: datetime
    booked: bool
    call_sid: Optional[str] = None
    created_at: datetime

# Utility functions
def get_available_slots(limit: int = 4) -> List[dict]:
    """Fetch available appointment slots from Supabase"""
    try:
        response = supabase.table("appointments")\
            .select("*")\
            .eq("booked", False)\
            .order("slot_time")\
            .limit(limit)\
            .execute()
        return response.data
    except Exception as e:
        logger.error(f"Error fetching available slots: {e}")
        return []

def book_appointment_slot(slot_id: int, call_sid: str) -> bool:
    """Mark a slot as booked in Supabase"""
    try:
        response = supabase.table("appointments")\
            .update({"booked": True, "call_sid": call_sid})\
            .eq("id", slot_id)\
            .eq("booked", False)\
            .execute()
        
        return len(response.data) > 0
    except Exception as e:
        logger.error(f"Error booking slot {slot_id}: {e}")
        return False

def send_whatsapp_notification(slot_details: dict, call_sid: str):
    """Send WhatsApp notification to reception"""
    try:
        slot_time = datetime.fromisoformat(slot_details['slot_time'].replace('Z', '+00:00'))
        formatted_time = slot_time.strftime("%B %d, %Y at %I:%M %p")
        
        message_body = (
            f"🎉 New Appointment Booked!\n\n"
            f"📅 Slot: {formatted_time}\n"
            f"📞 Call SID: {call_sid}\n"
            f"⏰ Booked at: {datetime.now().strftime('%I:%M %p')}"
        )
        
        message = twilio_client.messages.create(
            body=message_body,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=RECEPTION_WHATSAPP
        )
        
        logger.info(f"WhatsApp notification sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Error sending WhatsApp notification: {e}")
        return False

# Voice webhook endpoints
@app.post("/voice")
async def handle_voice_call(request: Request):
    """Handle incoming voice calls with speech recognition"""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    
    logger.info(f"Incoming call: {call_sid}")
    
    response = VoiceResponse()
    
    # Use speech recognition to capture user intent
    gather = Gather(
        input="speech",
        action="/process_query",
        method="POST",
        speech_timeout="3",
        language="en-US"
    )
    
    gather.say("Hello! Welcome to our appointment booking system. "
              "Please tell me how I can help you today. "
              "You can say things like 'book an appointment' or ask for assistance.")
    
    response.append(gather)
    
    # Fallback if no speech detected
    response.redirect("/fallback")
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/process_query")
async def process_speech_query(
    SpeechResult: str = Form(None),
    CallSid: str = Form("")
):
    """Process the speech input and determine next action"""
    
    logger.info(f"Speech result for call {CallSid}: {SpeechResult}")
    
    response = VoiceResponse()
    
    if not SpeechResult:
        response.say("I'm sorry, I didn't catch that. Let me transfer you to our reception.")
        response.redirect("/fallback")
        return Response(content=str(response), media_type="application/xml")
    
    # Check for booking intent
    booking_keywords = ["book", "appointment", "schedule", "reserve"]
    speech_lower = SpeechResult.lower()
    
    if any(keyword in speech_lower for keyword in booking_keywords):
        # Fetch available slots
        available_slots = get_available_slots(4)
        
        if not available_slots:
            response.say("I'm sorry, we don't have any available appointment slots at the moment. "
                        "Let me transfer you to our reception for assistance.")
            response.redirect("/fallback")
            return Response(content=str(response), media_type="application/xml")
        
        # Present available slots
        gather = Gather(
            num_digits=1,
            action="/book_slot",
            method="POST",
            timeout=10
        )
        
        slot_text = "Great! I found some available appointment slots. "
        
        for i, slot in enumerate(available_slots, 1):
            slot_time = datetime.fromisoformat(slot['slot_time'].replace('Z', '+00:00'))
            formatted_time = slot_time.strftime("%B %d at %I:%M %p")
            slot_text += f"Press {i} for {formatted_time}. "
        
        slot_text += "Or press 0 to speak with our reception."
        
        gather.say(slot_text)
        response.append(gather)
        
        # Store available slots in a way that can be retrieved (in a real app, use Redis or database)
        # For this demo, we'll handle it in the next endpoint
        
        response.say("I didn't receive your selection. Let me transfer you to our reception.")
        response.redirect("/fallback")
        
    else:
        response.say("I understand you need assistance. Let me connect you with our reception team.")
        response.redirect("/fallback")
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/book_slot")
async def book_appointment_slot_endpoint(
    Digits: str = Form(""),
    CallSid: str = Form("")
):
    """Handle slot booking based on user's digit selection"""
    
    logger.info(f"Slot selection for call {CallSid}: {Digits}")
    
    response = VoiceResponse()
    
    if Digits == "0":
        response.say("Connecting you to our reception team.")
        response.redirect("/fallback")
        return Response(content=str(response), media_type="application/xml")
    
    try:
        slot_number = int(Digits)
        if slot_number < 1 or slot_number > 4:
            raise ValueError("Invalid slot number")
        
        # Fetch available slots again (in production, cache this)
        available_slots = get_available_slots(4)
        
        if slot_number > len(available_slots):
            response.say("Invalid selection. Let me transfer you to our reception.")
            response.redirect("/fallback")
            return Response(content=str(response), media_type="application/xml")
        
        selected_slot = available_slots[slot_number - 1]
        
        # Book the slot
        if book_appointment_slot(selected_slot['id'], CallSid):
            slot_time = datetime.fromisoformat(selected_slot['slot_time'].replace('Z', '+00:00'))
            formatted_time = slot_time.strftime("%B %d, %Y at %I:%M %p")
            
            response.say(f"Perfect! I've successfully booked your appointment for {formatted_time}. "
                        f"You'll receive a confirmation message shortly. Thank you for choosing our service!")
            
            # Send WhatsApp notification
            send_whatsapp_notification(selected_slot, CallSid)
            
            response.hangup()
        else:
            response.say("I'm sorry, that slot is no longer available. "
                        "Let me transfer you to our reception for other options.")
            response.redirect("/fallback")
            
    except (ValueError, IndexError):
        response.say("Invalid selection. Let me transfer you to our reception.")
        response.redirect("/fallback")
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/fallback")
async def fallback_to_human(CallSid: str = Form("")):
    """Transfer call to human reception"""
    
    logger.info(f"Transferring call {CallSid} to fallback number")
    
    response = VoiceResponse()
    response.say("Please hold while I connect you to our reception team.")
    response.dial(FALLBACK_NUMBER)
    
    return Response(content=str(response), media_type="application/xml")

# REST API endpoints for React integration
@app.get("/api/slots", response_model=List[AppointmentSlot])
async def get_appointment_slots():
    """Get all available appointment slots for React frontend"""
    try:
        response = supabase.table("appointments")\
            .select("*")\
            .eq("booked", False)\
            .order("slot_time")\
            .execute()
        
        return response.data
    except Exception as e:
        logger.error(f"Error fetching slots for API: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch appointment slots")

@app.post("/api/book")
async def book_slot_api(request: BookSlotRequest):
    """Book an appointment slot via REST API"""
    try:
        # Use a generic call_sid for API bookings
        api_call_sid = f"API_{datetime.now().isoformat()}"
        
        if book_appointment_slot(request.slot_id, api_call_sid):
            # Get slot details for notification
            slot_response = supabase.table("appointments")\
                .select("*")\
                .eq("id", request.slot_id)\
                .execute()
            
            if slot_response.data:
                send_whatsapp_notification(slot_response.data[0], api_call_sid)
            
            return {"success": True, "message": "Appointment booked successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to book appointment - slot may already be taken")
    
    except Exception as e:
        logger.error(f"Error booking slot via API: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Voice Chatbot Appointment Booking API", "status": "healthy"}

@app.get("/health")
async def health_check():
    """Detailed health check"""
    try:
        # Test Supabase connection
        supabase.table("appointments").select("count", count="exact").execute()
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": {
                "supabase": "connected",
                "twilio": "configured"
            }
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(e)
        }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)