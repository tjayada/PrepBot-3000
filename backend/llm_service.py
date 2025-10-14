from typing import Dict, List, Any, Optional
from groq import Groq


def get_system_prompt() -> str:
    """Get the system prompt that defines the AI's personality."""
    return """You are a snappy, sassy chess coach who's basically a meme lord—think Hikaru Nakamura mixed with a roast comedian. Your job: Drop 2-3 sentences of brutally fun advice to demolish the opponent.

                CRITICAL REQUIREMENTS:
                - Exactly 2-3 sentences total—no more, no fluff
                - Be direct, actionable, and strategic with chess terms
                - Infuse cocky humor: Puns, exaggerations, light roasts
                - Focus on 1-2 key weaknesses but tie in others for flair
                - End with overconfident hype to pump up the user
                - Make it entertaining—lean into the absurdity of 'GM prep' for online chess

                Style: Punchy, witty, strategic. Turn data into gold; no boring lectures.
            """


# Add this static dict somewhere global, e.g., at the top of your script
FEATURE_DESCRIPTIONS = {
    "moves_pct_faster_than_avg": "Percentage of moves where the opponent moved faster than the optimal move time in the game. High values suggest they're confident or blitzing through familiar positions.",
    "opening_name": "The name of the opening played (e.g., 'English Defense'). Categorical, based on standard chess opening classifications.",
    "opening_move": "The specific opening move sequence (e.g., 'd4_b6'). Categorical, capturing early game choices.",
    "time_trouble": "Whether the opponent entered time trouble (low clock time) during the game. Boolean (True/False).",
    "game_length": "Total number of moves in the game. Numeric, indicating if they prefer short aggressive games or long endgames.",
    "opening_eco": "ECO code for the opening (e.g., 'A' for flank openings). Categorical.",
    "longest_slow_streak": "Longest streak of consecutive moves that took longer than average. Numeric, highlighting potential thinking bottlenecks.",
    "time_out": "Whether the game ended due to timeout. Boolean (True/False).",
    "num_critical_overthinks": "Number of moves in critical positions where they spent unusually long time. Numeric, spotting overthinking in key moments."
}

def build_context(insights: List[Dict[str, Any]], username: str, color: Optional[str], total_games: int) -> Dict[str, Any]:
    """Build context dictionary from insights."""
    return {
        'opponent_username': username,
        'opponent_color': color,
        'total_games_analyzed': total_games,
        'insights': insights,  # Keep all for potential depth
        'top_features': [insight['feature'] for insight in insights[:3]] if insights else [],
        'feature_descriptions': {insight['feature']: FEATURE_DESCRIPTIONS.get(insight['feature'], 'No description available') for insight in insights}
    }


def build_snappy_prompt(context: Dict[str, Any]) -> str:
    """Build the user prompt with context."""
    username = context['opponent_username']
    color = context.get('opponent_color', 'unknown') or 'various'  # Fallback for better readability
    insights = context['insights']
    descriptions = context['feature_descriptions']
    
    # Use top 5-6 for more material, but LLM will focus via system prompt
    top_insights = insights[:6]
    
    prompt = f"Yo, we're prepping to clown {username} in a Lichess blitz—because why not treat online randos like they're Carlsen? Analyzed {context['total_games_analyzed']} of their recent {color} games. Here's the juicy data on their habits (with explanations):\n\n"
    
    for insight in top_insights:
        feature = insight['feature']
        desc = descriptions.get(feature, 'N/A')
        insight_data = insight.get('insight', {})
        
        prompt += f"• {feature} ({desc}): "
        if insight.get('type') == 'categorical':
            good = insight_data.get('good', {})
            bad = insight_data.get('bad', {})
            prompt += f"Strong with '{good.get('value', 'N/A')}' (winrate {good.get('winrate', 0):.0%} over {good.get('games', 0)} games), weak with '{bad.get('value', 'N/A')}' (winrate {bad.get('winrate', 0):.0%} over {bad.get('games', 0)} games).\n"
        else:
            good = insight_data.get('good', {})
            bad = insight_data.get('bad', {})
            threshold = good.get('threshold', 'N/A')
            prompt += f"Better when {good.get('side', 'high/low')} (above/below {threshold}, winrate {good.get('winrate', 0):.0%} over {good.get('games', 0)} games), worse when {bad.get('side', 'high/low')} (winrate {bad.get('winrate', 0):.0%} over {bad.get('games', 0)} games).\n"
    
    prompt += f"\nNow, channel your inner trash-talking GM: Give me 2-3 punchy sentences to roast and crush {username}. Mix strategy with sass, throw in chess puns or memes for fun—make it feel like overkill for an online game."
    
    return prompt


def generate_llm_recommendations(
    system_prompt: str,
    prompt: str,
    groq_api_key: str,
) -> str:
    """
    Generate snappy and sassy chess recommendations using Groq LLM.
    
    Args:
        ...
        
    Returns:
        String containing snappy recommendations
    """
    if not groq_api_key:
        raise ValueError("GROQ_API_KEY is required")
    
    # Initialize Groq client
    client = Groq(api_key=groq_api_key)
    model = "llama-3.3-70b-versatile"
    
    
    try:
        # Generate response using Groq
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ],
            temperature=0.8,  # Good balance for personality and consistency
            max_tokens=150,   # Much shorter response
            top_p=0.9
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        print(f"Error generating LLM recommendations: {e}")
        return False