from pydantic import BaseModel, EmailStr
from typing import Optional, List


class ShareChatRequest(BaseModel):
    conversation_id: str  
    recipientEmail: EmailStr  


class ShareChatResponse(BaseModel):
    message: str  
    shareableLink: str  


class SharedChatResponse(BaseModel):
    chat: dict 