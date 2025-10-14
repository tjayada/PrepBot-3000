import asyncio
from chess import Status
from fastapi import Cookie, FastAPI, Request, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import httpx
import hashlib
import base64
import secrets
import dotenv
import os
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from postgrest import APIError
from supabase import create_client, Client
import berserk
import jwt
from typing import Optional, Dict
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import json

# Import our analysis modules
from analysis import generate_insights, stream_games_for_user, extract_features
from llm_service import build_context, build_snappy_prompt, generate_llm_recommendations, get_system_prompt


############### Setup ###############

"""
Database Schema Requirements:

1. users table:
   - id (text, primary key) - Lichess username
   - access_token (text) - Lichess API token
   - token_expires_at (timestamp) - Token expiration
   - session_expires_at (timestamp) - Session expiration
   - session_token (text) - JWT session token
   - created_at (timestamp) - Account creation time

2. analytics table:
   - game_id (text, primary key) - Unique game identifier
   - created_by (text) - User who created this analysis
   - target_user (text) - Opponent being analyzed
   - color (text) - Color preference for analysis
   - prep_text (text) - Analysis results
   - created_at (timestamp) - Analysis creation time
"""

dotenv.load_dotenv()
app = FastAPI()

SECRET_KEY = os.getenv("SECRET_KEY")
CLIENT_ID = os.getenv("LICHESS_CLIENT_ID")
BASE_URL = os.getenv(
    "BASE_URL", "http://localhost:8000"
)  # Default for my development for now
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")


supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")
if not CLIENT_ID:
    raise ValueError("LICHESS_CLIENT_ID environment variable is required")

# Add CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=60 * 60 * 24 * 30,  # 30 days
    same_site="lax",
    https_only=False,  # Set to False for local development
)

PORT = int(os.getenv("PORT", 8000))

executor = ThreadPoolExecutor(max_workers=10)
check_stream_events_queue = {}

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        self.active_connections[user_id] = websocket
        print(f"WebSocket connected for user: {user_id}")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            print(f"WebSocket disconnected for user: {user_id}")

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
                print(f"Sent WebSocket message to user {user_id}: {message}")
            except Exception as e:
                print(f"Error sending WebSocket message to {user_id}: {e}")
                self.disconnect(user_id)

manager = ConnectionManager()

############### Define Functions ###############


def base64url_encode(data: bytes) -> str:
    """Base64URL encode without padding"""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def create_verifier() -> str:
    """Create a code verifier for PKCE"""
    return base64url_encode(secrets.token_bytes(32))


def create_challenge(verifier: str) -> str:
    """Create a code challenge from verifier"""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64url_encode(digest)


def generate_session_token(user_id: str) -> str:
    """Generate a secure session token"""
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=30),  # 30 days expiry
        "iat": datetime.now(timezone.utc),
        "type": "session",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_session_token(token: str, user_id: str = None) -> Optional[dict]:
    """Verify and decode session token"""
    try:
        print(
            f"DEBUG: Verifying token with SECRET_KEY: {SECRET_KEY[:10] if SECRET_KEY else None}..."
        )
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        print(f"DEBUG: Token decoded successfully: {payload}")

        if payload.get("type") != "session":
            print(
                f"DEBUG: Token type mismatch. Expected 'session', got '{payload.get('type')}'"
            )
            return None

        if user_id:
            try:
                result = (
                    supabase.table("users")
                    .select("session_token")
                    .eq("id", user_id)
                    .execute()
                )
                if result.data and result.data[0].get("session_token") != token:
                    print("DEBUG: Token revoked - database token differs")
                    return None
            except Exception as e:
                print(f"DEBUG: Error checking token revocation: {e}")

        return payload
    except jwt.ExpiredSignatureError as e:
        print(f"DEBUG: Token expired: {e}")
        return None
    except jwt.InvalidTokenError as e:
        print(f"DEBUG: Invalid token: {e}")
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get current authenticated user from session token"""
    print("=== DEBUG: get_current_user called ===")
    print(f"All cookies: {dict(request.cookies)}")
    print(f"Session data: {dict(request.session)}")

    session_token = request.cookies.get("session_token")
    print(
        f"Session token from cookie: {session_token[:50] if session_token else None}..."
    )

    if not session_token:
        session_token = request.session.get("session_token")
        print(
            f"Session token from session: {session_token[:50] if session_token else None}..."
        )

    if not session_token:
        print("DEBUG: No session token found")
        return None

    print("DEBUG: Verifying session token...")
    token_payload = verify_session_token(session_token)
    if not token_payload:
        print("DEBUG: Token verification failed")
        return None

    print(f"DEBUG: Token payload: {token_payload}")

    # Get user data from database
    try:
        result = (
            supabase.table("users")
            .select("*")
            .eq("id", token_payload["user_id"])
            .execute()
        )
        print(f"DEBUG: Database query result: {result.data}")

        if result.data and len(result.data) > 0:
            user_data = result.data[0]
            print(f"DEBUG: User data from DB: {user_data}")

            if user_data.get("session_token") != session_token:
                print("DEBUG: Token revoked - database token differs")
                return None

            # Check if session hasn't expired in database
            if user_data.get("session_expires_at"):
                expires_at = datetime.fromisoformat(
                    user_data["session_expires_at"].replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                print(f"DEBUG: Session expires at: {expires_at}, Current time: {now}")
                if expires_at < now:
                    print("DEBUG: Session expired")
                    return None
            print("DEBUG: User authenticated successfully")
            return user_data
        else:
            print("DEBUG: No user data found in database")
    except Exception as e:
        print(f"DEBUG: Error fetching user data: {e}")

    return None


def require_auth(request: Request) -> dict:
    """Dependency to require authentication for protected routes"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def get_lichess_token(auth_code: str, verifier: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access token"""
    token_data = {
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code": auth_code,
        "code_verifier": verifier,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://lichess.org/api/token",
            json=token_data,
            headers={"Content-Type": "application/json"},
        )
        return response.json()


async def get_lichess_user(access_token: str) -> dict:
    """Get user information from Lichess API"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://lichess.org/api/account",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return response.json()


def get_lichess_client(user_data: dict) -> berserk.Client:
    """Get authenticated Lichess client using user data from database"""
    access_token = user_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No valid access token")

    session = berserk.TokenSession(access_token)
    return berserk.Client(session=session)


async def generate_prep(opponent_id: str, opponent_color: str, user_access_token: str = None) -> str:
    """
    Generate comprehensive preparation analysis for an opponent using LLM for snappy recommendations.
    
    Args:
        opponent_id: Username of the opponent to analyze
        opponent_color: Color the opponent will play ('white' or 'black')
        user_access_token: Access token for Lichess API calls
    
    Returns:
        String containing actionable preparation advice
    """
    try:
        print(f"Starting analysis for opponent: {opponent_id} ({opponent_color})")
        
        if not user_access_token:
            raise ValueError("No user access token provided")
        
        # Step 1: Fetch and analyze games
        max_games = 50 
        all_feats = []
        
        async for game in stream_games_for_user(opponent_id, user_access_token, opponent_color, max_games=max_games):
            feats = extract_features(game, opponent_color)
            if feats:
                all_feats.append(feats)
        
        if not all_feats:
            return f"⚠️ No recent games found for {opponent_id}. Cannot generate preparation analysis."
        
        df = pd.DataFrame(all_feats)
        print(f"Analyzed {len(df)} games for {opponent_id}")
        
        # Step 2: Generate insights 
        insights = generate_insights(df, min_info_gain=0.0, top_k=5)

        print(f"DEBUG : Generated insights for {opponent_id}: {insights}")

        if not insights:
            raise ValueError("No insights generated")

        # Step 3: Build context and prompts
        context = build_context(insights, opponent_id, opponent_color, len(df))
        prompt = build_snappy_prompt(context)
        system_prompt = get_system_prompt()

        # Step 4: generate recommendations
        quick_analysis = generate_llm_recommendations(system_prompt=system_prompt, prompt=prompt, groq_api_key=GROQ_API_KEY)
        
        if not quick_analysis:
            raise ValueError("No recommendations generated")
        
        return quick_analysis
        

    except Exception as e:
        print(f"Error generating prep for {opponent_id}: {e}")
        return f"❌ Could not analyze {opponent_id}: {str(e)}"


def listen_for_games_blocking(user: dict, loop: asyncio.AbstractEventLoop):
    user_id = user["id"]
    client = get_lichess_client(user)

    try:
        for event in client.board.stream_incoming_events():
            # check if not expired or canceled
            if user_id in check_stream_events_queue:
                if check_stream_events_queue[user_id]["expiry"] < datetime.now(
                    timezone.utc
                ):
                    # remove from queue
                    check_stream_events_queue.pop(user_id, None)
                    print(f"DEBUG: Listening expired for {user_id}")
                    break

            if user_id not in check_stream_events_queue:
                print(f"DEBUG: User not in activity queue for {user_id}")
                break

            print(f"DEBUG: Received event for user_id {user_id}: {event}")

            if event.get("type") == "gameStart":
                game_id = event["game"]["fullId"]
                opponent_id = str(event["game"]["opponent"]["id"])
                user_color = event["game"]["color"]
                opponent_color = "white" if user_color == "black" else "black"


                # Get user's access token for API calls
                user_access_token = user.get("access_token")
                
                # Schedule the async part to run on the main event loop
                asyncio.run_coroutine_threadsafe(
                    handle_game_start_event(game_id, user_id, opponent_id, opponent_color, user_access_token), loop
                )
                # You could optionally wait for the result, but it's not necessary here
                # future.result()

                print("\n DEBUG: Game start event handled successfully \n")

    except Exception as e:
        print(f"DEBUG: Stream error for {user_id}: {e}")
        # cancel activity from queue
        check_stream_events_queue.pop(user_id, None)
        # Raising HTTPException here will crash the thread, not send a response.
        # It's better to just log the error.


'''
async def stream_games_for_user(username: str, token: str, max_games: int = 50):
    """
    Asynchronously streams games from the Lichess API using httpx, yielding
    each game as it arrives. This is a highly memory-efficient and
    performant method.
    """
    url = f"https://lichess.org/api/games/user/{username}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/x-ndjson"  # Important for streaming
    }
    params = {
        "rated": "true",
        "clocks": "true",
        "opening": "true",
        "max": max_games,
        "perfType": "blitz,rapid,classical"
    }

    print(f"Streaming last {max_games} games for {username}...")
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, headers=headers, params=params, timeout=30.0) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        yield json.loads(line)
    except httpx.RequestError as e:
        print(f"An HTTP error occurred while streaming games: {e}")


async def analyze_opponent_games(opponent_id: str, token: str, max_games: int = 50) -> str:
    """
    Downloads, analyzes, and generates a brief for an opponent.
    """
    analyzer = ChessGameAnalyzer()
    games_analyzed = 0
    async for game in stream_games_for_user(opponent_id, token, max_games=max_games):
        analyzer.add_game(game)
        games_analyzed += 1
    
    if games_analyzed == 0:
        return "Could not fetch any recent games for this opponent."

    # This part is CPU-bound, but it runs after the I/O is complete.
    # For very large analysis, you could run this in an executor.
    brief = analyzer.generate_opponent_brief(opponent_id)
    return brief
'''

async def handle_game_start_event(game_id: str, user_id: str, opponent_id: str, opponent_color: str, user_access_token: str = None):
    """Asynchronous handler for game start events."""

    print("\n in the handle_game_start_event function")

    # Check DB if a recent analysis already exists
    #result = supabase.table("analytics").select("*").eq("target_user", opponent_id).execute()
    result = supabase.table("analytics").select("*").eq("target_user", opponent_id).eq("color", opponent_color).execute()

    if result.data and len(result.data) > 0:
        user_data = result.data[0]
        try:
            # Parse the datetime string and ensure it's timezone-aware
            created_at_str = user_data["created_at"]
            if created_at_str.endswith('Z'):
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            else:
                created_at = datetime.fromisoformat(created_at_str)
            
            # Ensure both datetimes are timezone-aware for comparison
            now = datetime.now(timezone.utc)
            if created_at.tzinfo is None:
                # If created_at is naive, assume it's UTC
                created_at = created_at.replace(tzinfo=timezone.utc)
            
            if created_at > now - timedelta(days=7):
                print(f"\nANALYSIS ALREADY EXISTS IN DB for opponent {opponent_id} and color {opponent_color}")
                return user_data["prep_text"]
        except (ValueError, TypeError) as e:
            print(f"Error parsing datetime for existing analysis: {e}, generating new analysis")


    # Fetch games, analyze, and generate the preparation text
    prep_text = await generate_prep(opponent_id, opponent_color, user_access_token)

    # Store in Supabase
    data = {
        "game_id": game_id,
        "created_by": user_id,
        "target_user": opponent_id,
        "color": opponent_color,
        "prep_text": prep_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    print(f"About to upsert analysis for opponent {opponent_id}")

    try:
        supabase.table("analytics").upsert(data, on_conflict="game_id").execute()
        print(f"DEBUG: Generated prep for opponent {opponent_id} in game {game_id}")
        
        # Send WebSocket notification to the user about new analysis
        notification = {
            "type": "new_analysis",
            "opponent": opponent_id,
            "color": opponent_color,
            "game_id": game_id,
            "message": f"New analysis completed for {opponent_id} ({opponent_color})"
        }
        await manager.send_personal_message(notification, user_id)
        
    except APIError as e:
        print(f"DEBUG: Database error: {e}")


############### Define API Endpoints ###############


@app.get("/auth/login")
async def login(request: Request):
    """Initiate OAuth login with Lichess"""
    base_url = (
        BASE_URL
        if BASE_URL != "http://localhost:8000"
        else f"{request.url.scheme}://{request.url.netloc}"
    )

    verifier = create_verifier()
    challenge = create_challenge(verifier)

    request.session["code_verifier"] = verifier

    oauth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{base_url}/auth/callback",
        # "scope": "preference:read stream:watch",
        "scope": "preference:read challenge:read",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }

    oauth_url = f"https://lichess.org/oauth?{urlencode(oauth_params)}"
    return RedirectResponse(oauth_url)


@app.get("/auth/callback")
async def callback(request: Request, code: str = None):
    """Handle OAuth callback from Lichess"""
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code provided")

    verifier = request.session.get("code_verifier")
    if not verifier:
        raise HTTPException(status_code=400, detail="No code verifier found in session")

    base_url = (
        BASE_URL
        if BASE_URL != "http://localhost:8000"
        else f"{request.url.scheme}://{request.url.netloc}"
    )
    redirect_uri = f"{base_url}/auth/callback"

    lichess_token = await get_lichess_token(code, verifier, redirect_uri)

    if not lichess_token.get("access_token"):
        raise HTTPException(status_code=400, detail="Failed getting token")

    access_token = lichess_token["access_token"]
    lichess_user = await get_lichess_user(access_token)
    user_id = lichess_user.get("username")
    if not user_id:
        raise HTTPException(status_code=400, detail="Failed getting user info")

    now = datetime.now(timezone.utc)
    token_expires_at = now + timedelta(hours=1)
    session_expires_at = now + timedelta(days=30)  # Long-lived session

    # Generate secure session token
    session_token = generate_session_token(user_id)
    print(f"DEBUG: Generated session token: {session_token[:50]}...")

    data = {
        "id": user_id,
        "access_token": access_token,
        "token_expires_at": token_expires_at.isoformat(),
        "session_expires_at": session_expires_at.isoformat(),
        "session_token": session_token,  # Store session token in DB
        "created_at": now.isoformat(),
    }
    print(f"DEBUG: User data to store: {data}")

    try:
        result = supabase.table("users").upsert(data, on_conflict="id").execute()
        print(f"DEBUG: Database upsert result: {result.data}")
    except APIError as e:
        print(f"DEBUG: Database error: {e}")
        raise HTTPException(status_code=500, detail=f"Supabase error: {e.message}")

    # Clean up OAuth session data
    request.session.pop("code_verifier", None)

    print(f"DEBUG: Generated session token for user: {user_id}")  # Debug print

    # Create response with HTTP-only cookie
    response = RedirectResponse(f"{FRONTEND_URL}/dashboard", status_code=303)

    # Set HTTP-only cookie for session token (more secure than session storage)
    print(f"DEBUG: Setting cookie with session token: {session_token[:50]}...")
    response.set_cookie(
        key="session_token",
        value=session_token,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,  # Prevent XSS attacks
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",  # CSRF protection
        domain=None,  # Allow cross-origin cookies
    )
    print("DEBUG: Cookie set successfully, redirecting to dashboard")

    return response
    # return {"message": f"Logged in as {user_id}, lichess token: {access_token}"}




@app.post("/api/start_listening")
async def start_listening(request_data: dict, user: dict = Depends(require_auth)):
    user_id = user["id"]
    duration = request_data.get("duration", 20)
    loop = asyncio.get_running_loop()
    expiry = datetime.now(timezone.utc) + timedelta(minutes=duration)

    # check if user / task not yet in activity queue, else skip
    if user_id not in check_stream_events_queue:
        check_stream_events_queue[user_id] = {
            "expiry": expiry,
            "duration": duration,
            "start_time": datetime.now(timezone.utc)
        }

        # Submit the blocking function to the executor
        executor.submit(listen_for_games_blocking, user, loop)

        return {"success": True, "message": f"Listening for {duration} minutes"}
    else:
        return {"success": True, "message": "Already listening"}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    """Logout user and invalidate session"""
    try:
        # Get current user to invalidate their session
        user = get_current_user(request)
        if user:
            # remove from activity queue
            check_stream_events_queue.pop(user["id"], None)
            # Invalidate session in database by setting expired time AND clearing token
            now = datetime.now(timezone.utc)
            supabase.table("users").update(
                {
                    "session_expires_at": now.isoformat(),
                    "session_token": None,  # Revoke the token
                }
            ).eq("id", user["id"]).execute()

            print(f"Logged out user: {user['id']}")

        # Clear the HTTP-only cookie
        response.delete_cookie(
            key="session_token",
            httponly=True,
            secure=False,  # Set to True in production with HTTPS
            samesite="lax",
            domain=None,
        )

        return {"success": True, "message": "Logged out successfully"}

    except Exception as e:
        print(f"Logout error: {e}")
        # Still clear the cookie even if DB update fails
        response.delete_cookie(
            key="session_token",
            httponly=True,
            secure=False,
            samesite="lax",
            domain=None,
        )
        return {"success": True, "message": "Logged out successfully"}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Check authentication status"""
    user = get_current_user(request)
    if user:
        return {"authenticated": True, "user_id": user["id"]}
    else:
        return {"authenticated": False}


@app.get("/api/debug/cookies")
async def debug_cookies(request: Request):
    """Debug endpoint to check cookies and session data"""
    return {
        "cookies": dict(request.cookies),
        "session": dict(request.session),
        "headers": dict(request.headers),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, session_token: Optional[str] = Cookie(None)):
    """WebSocket endpoint for real-time analysis notifications"""
    # Verify the user is authenticated by checking their session token
    user_data = verify_session_token(session_token)
    if not user_data:
        # Reject the connection if authentication fails
        await websocket.close(code=Status.WS_1008_POLICY_VIOLATION)
        return
    
    # Accept the WebSocket connection only if authentication succeeded
    await websocket.accept()
    
    user_id = user_data["user_id"]

    await manager.connect(websocket, user_id)
    try:
        while True:
            # Keep the connection alive and handle any incoming messages
            #data = await websocket.receive_text()
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        print(f"WebSocket error for user {user_id}: {e}")
        manager.disconnect(user_id)


@app.post("/api/cancel_listening")
async def cancel_listening(user: dict = Depends(require_auth)):
    user_id = user["id"]

    # remove from activity queue
    check_stream_events_queue.pop(user_id, None)

    return {"success": True, "message": "Stopped listening"}


@app.get("/api/listening_status")
async def get_listening_status(user: dict = Depends(require_auth)):
    """Get current listening status and remaining time"""
    user_id = user["id"]
    
    if user_id in check_stream_events_queue:
        queue_data = check_stream_events_queue[user_id]
        expiry_time = queue_data["expiry"]
        duration = queue_data.get("duration", 20)
        
        # Calculate remaining time
        now = datetime.now(timezone.utc)
        remaining_time = expiry_time - now
        
        if remaining_time.total_seconds() > 0:
            # Still listening
            return {
                "success": True,
                "listening": True,
                "expiry_time": expiry_time.isoformat(),
                "remaining_seconds": int(remaining_time.total_seconds()),
                "duration": duration
            }
        else:
            # Expired, remove from queue
            check_stream_events_queue.pop(user_id, None)
    
    # Not listening
    return {
        "success": True,
        "listening": False,
        "expiry_time": None,
        "remaining_seconds": 0,
        "duration": 0
    }

@app.post("/api/analyze_user")
async def analyze_user(request_data: dict, user: dict = Depends(require_auth)):
    """Analyze the current user for both white and black pieces"""
    try:
        user_id = user["id"]
        color = request_data.get("color", "white")  # "white" or "black"
        force_refresh = request_data.get("force_refresh", False)
        
        # Check if analysis already exists in DB and is recent (within 7 days)
        if not force_refresh:
            result = supabase.table("analytics").select("*").eq("target_user", user_id).eq("color", color).execute()
            
            if result.data and len(result.data) > 0:
                analysis_data = result.data[0]
                try:
                    # Parse the datetime string and ensure it's timezone-aware
                    created_at_str = analysis_data["created_at"]
                    if created_at_str.endswith('Z'):
                        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    else:
                        created_at = datetime.fromisoformat(created_at_str)
                    
                    # Ensure both datetimes are timezone-aware for comparison
                    now = datetime.now(timezone.utc)
                    if created_at.tzinfo is None:
                        # If created_at is naive, assume it's UTC
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    
                    if created_at > now - timedelta(days=7):
                        print(f"Using existing analysis for user {user_id} ({color})")
                        return {
                            "success": True,
                            "user_id": user_id,
                            "color": color,
                            "preparation": analysis_data["prep_text"],
                            "analyzed_at": analysis_data["created_at"],
                            "cached": True
                        }
                except (ValueError, TypeError) as e:
                    print(f"Error parsing datetime for existing analysis: {e}, generating new analysis")
        
        # Get user's access token
        user_access_token = user.get("access_token")
        if not user_access_token:
            raise HTTPException(status_code=401, detail="No valid access token")
        
        # Generate preparation analysis for the user themselves
        prep_text = await generate_prep(user_id, color, user_access_token)
        
        # create random game_id since no game is active
        game_id = secrets.token_urlsafe(16)
        
        # Store in Supabase
        data = {
            "game_id": game_id,
            "target_user": user_id,
            "created_by": user_id,
            "color": color,
            "prep_text": prep_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        
        try:
            supabase.table("analytics").upsert(data, on_conflict="game_id").execute()
            print(f"Stored user analysis for {user_id} ({color})")
        except APIError as e:
            print(f"Database error storing user analysis: {e}")
        
        return {
            "success": True,
            "user_id": user_id,
            "color": color,
            "preparation": prep_text,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "cached": False
        }
        
    except Exception as e:
        print(f"Error in analyze_user: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/analysis")
async def get_user_analysis(user: dict = Depends(require_auth)):
    """Get existing user analysis from database for both colors"""
    try:
        user_id = user["id"]
        
        # Fetch both white and black analysis
        result = supabase.table("analytics").select("*").eq("target_user", user_id).execute()
        
        analysis_data = {
            "white": None,
            "black": None
        }
        
        if result.data:
            for analysis in result.data:
                color = analysis["color"]
                if color in analysis_data:
                    # Check if analysis is recent (within 7 days)
                    try:
                        # Parse the datetime string and ensure it's timezone-aware
                        created_at_str = analysis["created_at"]
                        if created_at_str.endswith('Z'):
                            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        else:
                            created_at = datetime.fromisoformat(created_at_str)
                        
                        # Ensure both datetimes are timezone-aware for comparison
                        now = datetime.now(timezone.utc)
                        if created_at.tzinfo is None:
                            # If created_at is naive, assume it's UTC
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        
                        if created_at > now - timedelta(days=7):
                            analysis_data[color] = {
                                "preparation": analysis["prep_text"],
                                "analyzed_at": analysis["created_at"],
                                "cached": True
                            }
                    except (ValueError, TypeError) as e:
                        print(f"Error parsing datetime for analysis {analysis.get('game_id', 'unknown')}: {e}")
                        continue
        
        return {
            "success": True,
            "user_id": user_id,
            "analysis": analysis_data
        }
        
    except Exception as e:
        print(f"Error fetching user analysis: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/analysis/history")
async def get_user_analysis_history(user: dict = Depends(require_auth)):
    """Get all analysis results created by the user (for opponents only, excluding self-analysis)"""
    try:
        user_id = user["id"]
        
        # Fetch all analysis records where this user is the creator but target_user is different (opponents only)
        result = supabase.table("analytics").select("*").eq("created_by", user_id).neq("target_user", user_id).order("created_at", desc=True).execute()
        
        analysis_history = []
        
        if result.data:
            for analysis in result.data:
                analysis_history.append({
                    "game_id": analysis["game_id"],
                    "opponent": analysis["target_user"],
                    "color": analysis["color"],
                    "preparation": analysis["prep_text"],
                    "created_at": analysis["created_at"],
                    "analyzed_at": analysis["created_at"]  # For compatibility
                })
        
        return {
            "success": True,
            "user_id": user_id,
            "analysis_history": analysis_history,
            "total_count": len(analysis_history)
        }
        
    except Exception as e:
        print(f"Error fetching user analysis history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/public/analysis")
async def get_all_public_analysis():
    """Get all analysis results from the database (public endpoint, no authentication required)"""
    try:
        # Fetch all analysis records from the database, ordered by most recent first
        result = supabase.table("analytics").select("*").order("created_at", desc=True).execute()
        
        analysis_history = []
        
        if result.data:
            for analysis in result.data:
                analysis_history.append({
                    "game_id": analysis["game_id"],
                    "created_by": analysis["created_by"],
                    "opponent": analysis["target_user"],
                    "color": analysis["color"],
                    "preparation": analysis["prep_text"],
                    "created_at": analysis["created_at"],
                    "analyzed_at": analysis["created_at"]  # For compatibility
                })
        
        return {
            "success": True,
            "analysis_history": analysis_history,
            "total_count": len(analysis_history)
        }
        
    except Exception as e:
        print(f"Error fetching public analysis data: {e}")
        raise HTTPException(status_code=500, detail=str(e))



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
