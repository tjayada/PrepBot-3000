from typing import Dict, List, Any, Optional
from groq import Groq


def get_system_prompt() -> str:
    """Get the system prompt that defines the AI's personality."""
    return """  You are a snappy, sassy chess coach who gives brutally concise advice. You have exactly 2-3 sentences to help someone beat their opponent.

                CRITICAL REQUIREMENTS:
                - Maximum 2-3 sentences total
                - Be direct and actionable
                - Use chess terminology naturally
                - Be slightly cocky but helpful
                - Focus on the most important weakness to exploit
                - End with confidence

                Style: Short, punchy, strategic. No fluff, just pure tactical advice.
            """


def build_context(insights: List[Dict[str, Any]], username: str, color: Optional[str], total_games: int) -> Dict[str, Any]:
    """Build context dictionary from insights."""
    return {
        'opponent_username': username,
        'opponent_color': color,
        'total_games_analyzed': total_games,
        'insights': insights,
        'top_features': [insight['feature'] for insight in insights[:3]] if insights else []
    }


def build_snappy_prompt(context: Dict[str, Any]) -> str:
    """Build the user prompt with context."""
    username = context['opponent_username']
    color = context.get('opponent_color', 'unknown')
    insights = context['insights']
    
    # Focus on top 2-3 most important insights for brevity
    top_insights = insights[:3]
    
    prompt = f"Here's {username}'s main weaknesses from {color} games:\n\n"
    
    # Add only the most critical insights
    for insight in top_insights:
        #feature = insight['feature']
        insight_data = insight.get('insight', {})
        
        if insight.get('type') == 'categorical':
            good = insight_data.get('good', {})
            bad = insight_data.get('bad', {})
            prompt += f"• Strong: {good.get('value', 'N/A')} ({good.get('winrate', 0):.0%}), Weak: {bad.get('value', 'N/A')} ({bad.get('winrate', 0):.0%})\n"
        else:
            good = insight_data.get('good', {})
            bad = insight_data.get('bad', {})
            prompt += f"• Better when {good.get('side', 'N/A')}, worse when {bad.get('side', 'N/A')}\n"
    
    prompt += f"\nGive me 2-3 sentences to crush {username}. Be direct and strategic."
    
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
    model = "llama-3.1-8b-instant"
    
    
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
            temperature=0.7,  # Good balance for personality and consistency
            max_tokens=150,   # Much shorter response
            top_p=0.9
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        print(f"Error generating LLM recommendations: {e}")
        return False