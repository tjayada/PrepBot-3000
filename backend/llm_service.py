from typing import Dict, List, Any, Optional
from groq import Groq
import logging

logger = logging.getLogger(__name__)

# some extra context for eco types
eco_categories = {
    "A": {
        "name": "Flank Openings",
        "first_move": "Various (1.c4, 1.f4, 1.Nf3, 1.g3, etc.)",
        "code_range": "A00-A99",
        "description": "Covers all openings not starting with 1.e4 or 1.d4, including English Opening, Bird's Opening, Reti Opening, and various irregular openings.",
        "main_families": [
            "English Opening (1.c4)",
            "Bird's Opening (1.f4)",
            "Reti Opening (1.Nf3)",
            "King's Indian Attack",
            "Irregular and rare openings",
        ],
        "typical_characteristics": "Flexible, hypermodern approach; delayed central occupation; often transposes to other openings; emphasis on piece control rather than pawn control of center.",
        "style": "Strategic, positional, flexible",
    },
    "B": {
        "name": "Semi-Open Games",
        "first_move": "1.e4 (Black responds with anything except 1...e5)",
        "code_range": "B00-B99",
        "description": "Black meets 1.e4 with asymmetrical responses, leading to unbalanced positions with chances for both sides.",
        "main_families": [
            "Sicilian Defense (1.e4 c5) - B20-B99",
            "French Defense (1.e4 e6)",
            "Caro-Kann Defense (1.e4 c6)",
            "Pirc Defense (1.e4 d6)",
            "Alekhine's Defense (1.e4 Nf6)",
            "Scandinavian Defense (1.e4 d5)",
        ],
        "typical_characteristics": "Asymmetrical pawn structures; dynamic imbalances; Black often fights for counterplay rather than equality; rich in tactical and strategic complexity.",
        "style": "Sharp, tactical, unbalanced, fighting",
    },
    "C": {
        "name": "Open Games",
        "first_move": "1.e4 e5",
        "code_range": "C00-C99",
        "description": "Classical open games where both sides immediately stake a claim in the center with e-pawns.",
        "main_families": [
            "Italian Game (1.e4 e5 2.Nf3 Nc6 3.Bc4)",
            "Spanish/Ruy Lopez (1.e4 e5 2.Nf3 Nc6 3.Bb5)",
            "Scotch Game (1.e4 e5 2.Nf3 Nc6 3.d4)",
            "King's Gambit (1.e4 e5 2.f4)",
            "Vienna Game (1.e4 e5 2.Nc3)",
            "Petrov Defense (1.e4 e5 2.Nf3 Nf6)",
            "Philidor Defense (1.e4 e5 2.Nf3 d6)",
        ],
        "typical_characteristics": "Open center with early tactical possibilities; rapid piece development; often features king safety as a key theme; rich classical theory dating back centuries.",
        "style": "Tactical, classical, sharp, open positions",
    },
    "D": {
        "name": "Closed Games",
        "first_move": "1.d4 d5",
        "code_range": "D00-D99",
        "description": "Queen's pawn openings where Black immediately challenges the center with 1...d5.",
        "main_families": [
            "Queen's Gambit Declined (1.d4 d5 2.c4 e6)",
            "Queen's Gambit Accepted (1.d4 d5 2.c4 dxc4)",
            "Slav Defense (1.d4 d5 2.c4 c6)",
            "Semi-Slav Defense",
            "Tarrasch Defense (1.d4 d5 2.c4 e6 3.Nc3 c5)",
            "Albin Counter-Gambit",
            "Various d4 systems without c4",
        ],
        "typical_characteristics": "Slower, more strategic battles; pawn chains and structures play crucial roles; less tactical than open games but rich in positional themes; endgames are often important.",
        "style": "Strategic, positional, closed/semi-closed structures",
    },
    "E": {
        "name": "Indian Defenses",
        "first_move": "1.d4 Nf6",
        "code_range": "E00-E99",
        "description": "Hypermodern defenses where Black develops the knight before committing central pawns, allowing White to build a pawn center.",
        "main_families": [
            "King's Indian Defense (1.d4 Nf6 2.c4 g6)",
            "Nimzo-Indian Defense (1.d4 Nf6 2.c4 e6 3.Nc3 Bb4)",
            "Queen's Indian Defense (1.d4 Nf6 2.c4 e6 3.Nf3 b6)",
            "Grunfeld Defense (1.d4 Nf6 2.c4 g6 3.Nc3 d5)",
            "Benoni Defense (1.d4 Nf6 2.c4 c5)",
            "Bogo-Indian Defense (1.d4 Nf6 2.c4 e6 3.Nf3 Bb4+)",
            "Catalan Opening",
        ],
        "typical_characteristics": "Hypermodern strategy; Black allows White to establish pawn center then attacks it with pieces; complex middlegame play; often features opposite-side castling and pawn storms.",
        "style": "Hypermodern, complex, strategic depth, often sharp",
    },
}


def get_system_prompt() -> str:
    """Get the system prompt that defines the AI's personality."""
    return """You are a witty chess coach robot creating pre-game prep advice, think somewhere between Hikaru Nakamura and Karen from Spongebob. Your goal: Give 2-3 sentences of entertaining, sometimes sarcastic, ACTIONABLE strategy to exploit the opponent's weaknesses.

CRITICAL RULES:
- Exactly 2-3 short sentences total
- Name the opponent and their color
- Focus on EXPLOITING their weaknesses (avoid their strengths, target their weak spots)
- Be specific: Push them into [weak opening] or Avoid long games with more than 40 moves (xyz% winrate)
- Be a robot by using words like "BEEP BOOP" or other robot sounds or common phrases
- Add humor through puns, light roasts, or (sarcastic) exaggerations
- Make it sound like legit chess prep, just more entertaining

BAD example: "They're good at the Sicilian" (useless - doesn't help us)
GOOD example: "Avoid the Sicilian (70% winrate), instead steer into the Zukertort where they crumble (0% winrate)"

Turn data into a game plan, not a stats lecture."""


FEATURE_DESCRIPTIONS = {
    "moves_pct_faster_than_avg": "Percentage of moves faster than optimal - high means they blitz confidently",
    "opening_name": "Opening repertoire - which openings they play",
    "opening_move": "First move (if white) or move_pair (if black) preference (e.g., white: e4, d4, .. or black: e4_e5, e4_d5, ...)",
    "time_trouble": "Whether they hit time pressure ( less than 10 seconds on their clock)",
    "game_length": "Game length in moves, eg. 42 moves",
    "opening_eco": "ECO code for opening classification",
    "longest_slow_streak": "Longest streak of slow moves",
    "time_out": "Whether the game was won or lost on time",
    "num_critical_overthinks": "Times they think 2 times longer than the optimal move time",
}


def format_exploit_numeric(insight: Dict[str, Any]) -> str:
    """Format numeric insight as an exploitable weakness or strength to avoid."""
    feature = insight["feature"]
    data = insight["insight"]
    good = data["good"]
    bad = data["bad"]

    # Determine what the feature means for strategy
    if feature == "moves_pct_faster_than_avg":
        if good["side"] == "high":
            return f"Plays fast and confident (>{good['threshold']:.0%}, {good['winrate']:.0%} WR) but struggles when forced to think ({bad['winrate']:.0%} WR)"
        else:
            return f"Performs better when moving slower ({good['winrate']:.0%} WR) - plays poorly when playing rushed ({bad['winrate']:.0%} WR fast games)"

    elif feature == "game_length":
        if good["side"] == "high":
            return f"Strong in long games (>{good['threshold']:.0f} moves, {good['winrate']:.0%} WR) but weak in short tactical battles ({bad['winrate']:.0%} WR)"
        else:
            return f"Prefers short games (<{bad['threshold']:.0f} moves, {good['winrate']:.0%} WR in quick games) - vulnerable in long endgames ({bad['winrate']:.0%} WR)"

    elif feature == "num_critical_overthinks":
        if good["side"] == "low":
            return f"Makes intuitive moves or avoids complex positions ({good['winrate']:.0%} WR), but might calculate too long for certain positions ({bad['winrate']:.0%} WR)"
        else:
            return f"Better when calculating for a long time ({good['winrate']:.0%} WR), but struggles with intuitive moves ({bad['winrate']:.0%} WR)"

    # Generic fallback
    return f"Strong when {good['side']} {feature} ({good['winrate']:.0%} WR), weak when {bad['side']} ({bad['winrate']:.0%} WR)"


def format_exploit_categorical(insight: Dict[str, Any]) -> str:
    """Format categorical insight focusing on weaknesses to exploit."""
    feature = insight["feature"]
    data = insight["insight"]
    good = data["good"]
    bad = data["bad"]

    if feature == "opening_name":
        return f"Strong in {good['value']} ({good['winrate']:.0%} WR in {good['games']} games) - AVOID IT. Weak against {bad['value']} ({bad['winrate']:.0%} WR, {bad['games']} games) - EXPLOIT"

    elif feature == "opening_move":
        return f"Comfortable after 1.{good['value']} ({good['winrate']:.0%} WR, {good['games']} games) but struggles against 1.{bad['value']} ({bad['winrate']:.0%} WR, {bad['games']} games)"

    elif feature == "opening_eco":
        good_eco = good["value"]
        bad_eco = bad["value"]

        good_info = eco_categories.get(good_eco)
        bad_info = eco_categories.get(bad_eco)

        return (
            f"Strong in {good_info['name']} ({good['value']}: {good['winrate']:.0%} WR, {good['games']} games) - "
            f"{good_info['typical_characteristics']}, {good_info['style']} style. "
            f"Weak in {bad_info['name']} ({bad['value']}: {bad['winrate']:.0%} WR, {bad['games']} games) - EXPLOIT"
        )

    # time_trouble
    elif feature == "time_trouble":
        return f"In {good['games']} games they have won with not much time on their clocks ({good['winrate']:.0%} WR) - while in {bad['games']} games they have lost ({bad['winrate']:.0%} WR)"

    # time_out
    elif feature == "time_out":
        return f"In {good['games']} games they have won the games that ended in a timeout ({good['winrate']:.0%} WR) - while in {bad['games']} games they have lost ({bad['winrate']:.0%} WR) with a timeout"

    # Generic
    return f"Strong with {good['value']} ({good['winrate']:.0%} WR) - weak with {bad['value']} ({bad['winrate']:.0%} WR)"


def build_context(
    insights: List[Dict[str, Any]],
    username: str,
    color: Optional[str],
    total_games: int,
) -> Dict[str, Any]:
    """Build context dictionary from insights with clearer exploit-focused framing."""

    # Select most impactful insights
    # top_insights = insights[:3]

    top_insights = insights

    # Format each insight for clarity
    formatted_insights = []
    for insight in top_insights:
        if insight["type"] == "categorical":
            formatted = format_exploit_categorical(insight)
        else:
            formatted = format_exploit_numeric(insight)

        formatted_insights.append(
            {
                "feature": insight["feature"],
                "feature_description": FEATURE_DESCRIPTIONS.get(insight["feature"]),
                "summary": formatted,
                #'info_gain': insight['info_gain']
            }
        )

    return {
        "opponent_username": username,
        "opponent_color": color or "various colors",
        "total_games_analyzed": total_games,
        "formatted_insights": formatted_insights,
    }


def build_snappy_prompt(context: Dict[str, Any]) -> str:
    """Build the user prompt with clearer, exploit-focused context."""
    username = context["opponent_username"]
    color = context["opponent_color"]

    prompt = f"""Opponent: {username} (playing {color})
Games analyzed: {context["total_games_analyzed"]}

KEY WEAKNESSES TO EXPLOIT:
"""

    for i, insight in enumerate(context["formatted_insights"], 1):
        prompt += f"{i}. {insight['feature']}: {insight['summary']}\n"

    prompt += f"""
Create a 2-3 sentence game plan to beat {username}. Focus on:
- Which weaknesses to exploit (avoid their strengths!)
- Specific tactical advice (opening choices, game pace, etc.)
- Keep it entertaining and fun"""

    return prompt


def generate_llm_recommendations(
    system_prompt: str,
    prompt: str,
    groq_api_key: str,
) -> str:
    """
    Generate snappy and sassy chess recommendations using Groq LLM.

    Args:
        system_prompt: System instructions for the LLM
        prompt: User prompt with opponent data
        groq_api_key: Groq API key

    Returns:
        String containing snappy recommendations
    """
    if not groq_api_key:
        raise ValueError("GROQ_API_KEY is required")

    client = Groq(api_key=groq_api_key)
    # model = "llama-3.1-8b-instant"
    model = "llama-3.3-70b-versatile"

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
            max_tokens=150,
            top_p=0.9,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"Error generating LLM recommendations: {e}")
        return "Failed to generate recommendations - check your API key and connection."
