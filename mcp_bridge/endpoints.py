from fastapi import APIRouter

router = APIRouter(prefix='/v1')

@router.get('/completions')
async def completions():
    """Completions endpoint"""


@router.get('/chat/completions')
async def chat_completions():
    """Chat Completions endpoint"""
