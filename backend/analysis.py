import numpy as np
import pandas as pd

import httpx
import json
import asyncio


async def stream_games_for_user(username, token, color, max_games=100):
    """
    Asynchronously streams games from the Lichess API using httpx, yielding
    each game as it arrives.
    """
    url = f"https://lichess.org/api/games/user/{username}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/x-ndjson" 
    }
    params = {
        "rated": "true",
        "clocks": "true",
        "opening": "true",
        "color": color,
        "max": max_games,
        "perfType": "blitz,rapid,classical"
    }

    print(f"Fetching last {max_games} games for {username}...")

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, headers=headers, params=params, timeout=30.0) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        yield json.loads(line)
    except httpx.ConnectError as e:
        print("\n[Network Error] Could not connect to Lichess.org.")
        print(f"Please check your internet connection and firewall settings. Details: {e}")
    except httpx.RequestError as e:
        print(f"An HTTP error occurred: {e}")
        raise 


def extract_features(game_data, player_color):
    features = {}

    # result
    # first get status if draw set loss
    try:
        if game_data['status'] in ['draw', 'stalemate']:
            features['result'] = False
        else:
            features['result'] = game_data['winner'] == player_color

    except KeyError:
        return None



    # opening preferences
    try:
        features['opening_eco'] = game_data['opening']['eco'][0] # get main family
        features['opening_name'] = game_data['opening']['name'].split(':')[0] # get main line name
        features['opening_ply'] = game_data['opening']['ply']

        all_moves = game_data['moves'].split()

        #print(moves)

        if (player_color == 'white') and (len(all_moves) >= 2):
            # get first move pair
            features['opening_move'] = f"{all_moves[0]}_{all_moves[1]}"

        elif (player_color == 'black') and (len(all_moves) >= 1):
            # get first move
            features['opening_move'] = all_moves[0]
                    
        else:
            return None

    except KeyError:
        return None


    # game length (in full moves)
    moves = game_data['moves'].split()
    game_len = len(moves) // 2

    #game_len_cat = game_len_categories(game_len)
    features['game_length'] = game_len

    # time control and timeout flag
    initial_time = game_data['clock']['initial']
    #total_time = game_data['clock']['totalTime']
    increment = game_data['clock']['increment']
    features['time_out'] = game_data['status'] in ['outoftime', 'timeout']

    clocks = game_data['clocks']
    times_spent, remainings = get_move_times(player_color, clocks)

    features["time_trouble"] = True if remainings[-1] < 10 else False 

    if not times_spent:
        features.update({
            'moves_pct_faster_than_avg': 0.0,
            'longest_slow_streak': 0,
            'num_critical_overthinks': 0,
            #'moves_std': 0.0,
            #'moves_avg': 0.0,
            'time_trouble': False
        })
    else:
        move_time_avg_optimal = (initial_time + increment * game_len) / game_len if game_len > 0 else initial_time

        faster_than_avg_bools = [-(ts) < move_time_avg_optimal for ts in times_spent]

        pct_faster = sum(faster_than_avg_bools) / len(faster_than_avg_bools)

        # longest consecutive streak where they were slower-than-average
        longest_slow_streak = 0
        cur_streak = 0
        for flag in faster_than_avg_bools:
            if not flag:  # slower-than-average
                cur_streak += 1
                if cur_streak > longest_slow_streak:
                    longest_slow_streak = cur_streak
            else:
                cur_streak = 0

        # critical overthinks: times > 2 * expected average
        critical_overthinks = sum(1 for ts in times_spent if (-ts) > 2 * move_time_avg_optimal)

        # normalised by remaining clock before the move (safe division)
        #normed = []
        #for spent, rem in zip(times_spent, remainings):
        #    rem_safe = rem if rem and rem > 0 else max(rem, 1)  # fallback to 1s to avoid div0 if rem==0
        #    normed.append((-spent) / rem_safe)

        # stats on normalised times
        #moves_std = statistics.stdev(normed) if len(normed) > 1 else 0.0
        #moves_avg = statistics.mean(normed)

        features.update({
            'moves_pct_faster_than_avg': round(pct_faster, 2),
            'longest_slow_streak': longest_slow_streak,
            'num_critical_overthinks': critical_overthinks,
        #    'moves_std': round(moves_std, 2),
        #    'moves_avg': round(moves_avg, 2),
        })


    return features

def get_move_times(color: str, moves: list) -> list:
    """iterate over moves, calculate time difference to previous move and return a list of times"""
    move_times = []
    move_times_remaining = []
    if color == 'black':
        moves = moves[1:]
    
    for i, move in enumerate(moves):
        if i % 2 == 0:
            previous = moves[i-2] if i > 0 else None
            if previous:
                difference_centiseconds = move - previous
                difference_seconds = difference_centiseconds / 100
                move_times.append(difference_seconds)
                move_times_remaining.append(move / 100)
            
            else:
                move_times.append(0)
                move_times_remaining.append(move / 100)
    
    return move_times, move_times_remaining


def game_len_categories(x):
    # cat x between 10, 20, 30, etc
    return 10 * (x // 10)


def entropy(counts):
    """counts = array-like of class counts, e.g. [wins, losses]."""
    total = sum(counts)
    if total == 0:
        return 0
    probs = [c / total for c in counts if c > 0]
    return -sum(p * np.log2(p) for p in probs)


def info_gain_categorical(df, feature, target='result'):
    root_ent = entropy(df[target].value_counts())
    n = len(df)

    # group by feature and calculate weighted entropy
    weighted_ent = 0
    for _, group in df.groupby(feature, observed=False):
        counts = group[target].value_counts()
        weighted_ent += (len(group) / n) * entropy(counts)

    return root_ent - weighted_ent


def info_gain_numeric(df, feature, target='result'):
    root_ent = entropy(df[target].value_counts())
    n = len(df)

    values = sorted(df[feature].unique())
    thresholds = [(values[i] + values[i+1]) / 2 for i in range(len(values)-1)]

    best_ig = -1
    best_threshold = None

    for t in thresholds:
        left = df[df[feature] <= t]
        right = df[df[feature] > t]

        weighted_ent = (
            len(left)/n * entropy(left[target].value_counts()) +
            len(right)/n * entropy(right[target].value_counts())
        )
        ig = root_ent - weighted_ent

        if ig > best_ig:
            best_ig, best_threshold = ig, t

    return best_ig, best_threshold


def compute_information_gains(df):
    """Compute information gains for predefined features and return structured specs.

    Returns a list of dicts like:
    { 'feature': str, 'type': 'categorical'|'numeric', 'info_gain': float, 'threshold': Optional[float] }
    """
    specs = []

    categorical_features = [
        'opening_eco',
        'opening_name',
        'time_out',
        'time_trouble',
        'opening_move',
    ]

    for feature in categorical_features:
        if feature in df.columns:
            try:
                ig = info_gain_categorical(df, feature)
                specs.append({
                    'feature': feature,
                    'type': 'categorical',
                    'info_gain': float(ig),
                    'threshold': None,
                })
            except Exception:
                print("skip feature : ", feature)
                continue

    numeric_features = [
        'moves_pct_faster_than_avg',
        'longest_slow_streak',
        'num_critical_overthinks',
        'game_length'
    ]

    for feature in numeric_features:
        if feature in df.columns:
            try:
                ig, threshold = info_gain_numeric(df, feature)
                specs.append({
                    'feature': feature,
                    'type': 'numeric',
                    'info_gain': float(ig) if ig is not None else 0.0,
                    'threshold': float(threshold) if threshold is not None else None,
                })
            except Exception:
                print("skip feature : ", feature)
                continue

    # Order by info gain descending
    specs.sort(key=lambda s: s['info_gain'], reverse=True)
    return specs


def _summarize_categorical_feature(df, feature, min_games=3):
    """Return best/worst categories with winrates and counts.
    
    Uses Wilson score confidence interval lower bound to balance winrate with statistical
    uncertainty. This is statistically principled and favors categories with both high
    performance and sufficient sample size.
    
    Output:
    {
      'good': { 'value': <category>, 'winrate': float, 'games': int },
      'bad':  { 'value': <category>, 'winrate': float, 'games': int }
    }
    """
    # Aggregate win counts and totals
    grouped = df.groupby(feature, observed=False)['result'].agg(['sum', 'count']).rename(columns={'sum': 'wins', 'count': 'games'})
    if grouped.empty:
        return None
    
    # Calculate winrates
    grouped = grouped.assign(winrate=(grouped['wins'] / grouped['games']).fillna(0.0))
    
    # Filter by minimum games requirement
    valid_groups = grouped[grouped['games'] >= min_games]
    
    if len(valid_groups) < 2:
        # If not enough categories with min_games, try with lower threshold
        if min_games > 1:
            return _summarize_categorical_feature(df, feature, min_games=1)
        return None
    
    # Calculate Wilson score confidence interval lower bound
    # This is statistically principled and balances winrate with sample size uncertainty
    import numpy as np
    
    def wilson_lower_bound(wins, total, confidence=0.95):
        """
        Calculate the lower bound of Wilson score confidence interval for binomial proportion.
        
        Args:
            wins: number of wins (can be fractional)
            total: total number of games
            confidence: confidence level (default 0.95 for 95% confidence)
        
        Returns:
            Lower bound of confidence interval
        """
        if total == 0:
            return 0.0
        
        # z-score for desired confidence level
        if confidence == 0.95:
            z = 1.96
        elif confidence == 0.90:
            z = 1.645
        else:
            # Approximate z-score for other confidence levels
            z = 2.0 if confidence >= 0.95 else 1.5
        
        p = wins / total
        
        # Wilson score formula for lower bound
        denominator = 1 + (z**2 / total)
        numerator = p + (z**2 / (2 * total)) - z * np.sqrt((p * (1 - p) / total) + (z**2 / (4 * total**2)))
        
        return max(0.0, numerator / denominator)
    
    # Calculate Wilson lower bound for each category
    valid_groups = valid_groups.assign(
        wilson_lower_bound=valid_groups.apply(
            lambda row: wilson_lower_bound(row['wins'], row['games']), axis=1
        )
    )
    
    # Sort by Wilson lower bound (highest = best, lowest = worst)
    wilson_sorted = valid_groups.sort_values('wilson_lower_bound')
    
    # Return best and worst categories
    best_row = wilson_sorted.iloc[-1]  # Highest Wilson lower bound
    worst_row = wilson_sorted.iloc[0]  # Lowest Wilson lower bound
    
    return {
        'good': {
            'value': str(wilson_sorted.index[-1]),
            'winrate': round(float(best_row['winrate']), 3),
            'games': int(best_row['games']),
        },
        'bad': {
            'value': str(wilson_sorted.index[0]),
            'winrate': round(float(worst_row['winrate']), 3),
            'games': int(worst_row['games']),
        },
    }


def _summarize_numeric_feature(df, feature, threshold, min_games=3):
    """Split by threshold and report which side is better/worse.
    
    Splits data by threshold and reports winrates for each side, but only
    if both sides have enough games for statistical significance.
    
    Output:
    {
      'good': { 'side': 'low'|'high', 'threshold': float, 'winrate': float, 'games': int },
      'bad':  { 'side': 'low'|'high', 'threshold': float, 'winrate': float, 'games': int }
    }
    """
    if threshold is None:
        return None

    # Split data by threshold - use same logic as info_gain_numeric
    left = df[df[feature] <= threshold]
    right = df[df[feature] > threshold]
    
    left_games = len(left)
    right_games = len(right)
    
    # Require minimum games on both sides
    if left_games < min_games or right_games < min_games:
        # Try with lower threshold if possible
        if min_games > 1:
            return _summarize_numeric_feature(df, feature, threshold, min_games=1)
        return None
    
    # Calculate winrates
    left_wr = float(left['result'].mean()) if left_games > 0 else 0.0
    right_wr = float(right['result'].mean()) if right_games > 0 else 0.0

    # Choose good/bad by winrate
    if right_wr > left_wr:
        good = {
            'side': 'high',
            'threshold': float(threshold),
            'winrate': round(right_wr, 3),
            'games': right_games,
        }
        bad = {
            'side': 'low',
            'threshold': float(threshold),
            'winrate': round(left_wr, 3),
            'games': left_games,
        }
    else:
        good = {
            'side': 'low',
            'threshold': float(threshold),
            'winrate': round(left_wr, 3),
            'games': left_games,
        }
        bad = {
            'side': 'high',
            'threshold': float(threshold),
            'winrate': round(right_wr, 3),
            'games': right_games,
        }

    return { 'good': good, 'bad': bad }


def generate_insights(df, min_info_gain=0.10, top_k=3):
    """Generate actionable insights driven by highest information gains.

    Returns a list of items ordered by info gain:
    [
      {
        'feature': str,
        'info_gain': float,
        'type': 'categorical'|'numeric',
        'insight': { 'good': {...}, 'bad': {...} }
      }, ...
    ]
    """
    specs = compute_information_gains(df)
    print(f"Computed information gains for {len(specs)} features")

    # Apply threshold and top-k
    filtered = [s for s in specs if s['info_gain'] is not None and s['info_gain'] >= min_info_gain]
    print(f"Features with info_gain >= {min_info_gain}: {len(filtered)}")
    
    # If no features meet the threshold, relax it progressively
    if len(filtered) == 0 and len(specs) > 0:
        print(f"No features met threshold {min_info_gain}, relaxing...")
        for relaxed_threshold in [0.05, 0.01, 0.001, 0.0]:
            filtered = [s for s in specs if s['info_gain'] is not None and s['info_gain'] >= relaxed_threshold]
            if len(filtered) > 0:
                print(f"Using relaxed threshold {relaxed_threshold} -> {len(filtered)} features")
                break
    
    top = filtered[:top_k] if top_k is not None and top_k > 0 else filtered
    print(f"Processing top {len(top)} features for insights")

    insights = []
    for spec in top:
        feature = spec['feature']
        print(f"Processing feature: {feature} (IG={spec['info_gain']:.3f}, type={spec['type']})")
        if spec['type'] == 'categorical':
            summary = _summarize_categorical_feature(df, feature, min_games= len(df) // 20)
        else:
            summary = _summarize_numeric_feature(df, feature, spec.get('threshold'), min_games= len(df) // 20)

        if summary is None:
            print(f"  -> Skipped {feature}: Could not generate summary")
            continue

        print(f"  -> Success: {feature}")
        insights.append({
            'feature': feature,
            'type': spec['type'],
            'info_gain': round(float(spec['info_gain']), 3),
            'insight': summary,
        })

    print(f"Generated {len(insights)} insights from {len(top)} processed features")
    return insights


async def main():

    #import time
    #start_time = time.time()

    # check if df.csv exists
    import os
    if not os.path.isfile('df.csv'):
        print("df.csv not found. Fetching games from Lichess API...")

        token = "MY_TOKEN"
        #target = "Gogi78Sokol".lower()
        #target = "tjayada".lower()
        target = "chessiandoceo".lower()
        #color = "black
        color = "white"
        num = 100
        
        all_feats = []
        async for game in stream_games_for_user(target, token, color, max_games=num):
            feats = extract_features(game, color)
            if not feats:
                continue
            all_feats.append(feats)

        df = pd.DataFrame(all_feats)
        
        # save dataframe
        df.to_csv('df.csv', index=False)

        if df.empty:
            print("\nERROR: DataFrame is empty after fetching. This might be due to an API error or no recent games found. Exiting.")
            return

        print("DataFrame saved to df.csv")
    else:
        print("Loading DataFrame from df.csv")
        df = pd.read_csv('df.csv')
    
    # quick apply func that splits name at ":" and only uses first half
    #df['opening_name'] = df['opening_name'].apply(lambda x: x.split(':')[0])

    root_entropy = entropy(df['result'].value_counts())
    print("root entropy : ", round(root_entropy, 3))

    ig_opening = info_gain_categorical(df, 'opening_eco')
    print("Info Gain (opening_eco):", round(ig_opening, 3))

    ig_name = info_gain_categorical(df, 'opening_name')
    print("Info Gain (opening_name):", round(ig_name, 3))

    ig_time_out = info_gain_categorical(df, 'time_out')
    print("Info Gain (time_out):", round(ig_time_out, 3))

    ig_game_len, threshold = info_gain_numeric(df, 'game_length')
    #ig_game_len = info_gain_categorical(df, 'game_length')
    print("Info Gain (game_length):", round(ig_game_len, 3))

    ig_moves_avg, threshold = info_gain_numeric(df, 'moves_pct_faster_than_avg')
    print("Info Gain (moves_pct_faster_than_avg):", round(ig_moves_avg, 3))

    ig_longest_slow_streak, threshold = info_gain_numeric(df, 'longest_slow_streak')
    print("Info Gain (longest_slow_streak):", round(ig_longest_slow_streak, 3))

    ig_critical_overthinks, threshold = info_gain_numeric(df, 'num_critical_overthinks')
    print("Info Gain (num_critical_overthinks):", round(ig_critical_overthinks, 3))

    ig_burned_time = info_gain_categorical(df, 'time_trouble')
    print("Info Gain (burned_time):", round(ig_burned_time, 3))

    ig_opening_move = info_gain_categorical(df, 'opening_move')
    print("Info Gain (opening_move):", round(ig_opening_move, 3))

    #ig_opening_ply = info_gain_categorical(df, 'opening_ply')
    #ig_opening_ply, threshold = info_gain_numeric(df, 'opening_ply')
    #print("Info Gain (opening_ply):", round(ig_opening_ply, 3))

    print("\n\n")
 
    # Count wins/losses per opening_name
    dist = df.groupby('opening_name')['result'].value_counts().unstack(fill_value=0)

    dist['total'] = dist.sum(axis=1)
    dist['winrate'] = dist[True] / dist['total']

    #dist_sorted = dist.sort_values('winrate', ascending=False)

    # print all openings
    #print(dist_sorted)


    # print values of opening eco
    #print(df['opening_eco'].value_counts())

    #end_time = time.time()
    #elapsed_time = end_time - start_time
    #print("elapsed time:", elapsed_time)

    # Generate and persist insights based on highest information gains
    insights = generate_insights(df, min_info_gain=0.0, top_k=10)
    print("Top insights (by info gain):")
    print(json.dumps(insights, indent=2))

    with open('insights.json', 'w') as f:
        json.dump(insights, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())




    """
    root entropy :  0.999
Info Gain (opening_eco): 0.02
Info Gain (opening_name): 0.28
Info Gain (time_out): 0.047
Info Gain (game_length): 0.146
Info Gain (moves_pct_faster_than_avg): 0.208
Info Gain (longest_slow_streak): 0.067
Info Gain (num_critical_overthinks): 0.058
Info Gain (burned_time): 0.194
Info Gain (opening_move): 0.126
    """

    """ tjayada
    root entropy :  0.999
Info Gain (opening_eco): 0.052
Info Gain (opening_name): 0.126
Info Gain (time_out): 0.004
Info Gain (game_length): 0.027
Info Gain (moves_pct_faster_than_avg): 0.059
Info Gain (longest_slow_streak): 0.03
Info Gain (num_critical_overthinks): 0.083
Info Gain (burned_time): 0.041
Info Gain (opening_move): 0.114
    """

    """ chessiandoceo
    root entropy :  0.811
Info Gain (opening_eco): 0.099
Info Gain (opening_name): 0.389
Info Gain (time_out): 0.003
Info Gain (game_length): 0.069
Info Gain (moves_pct_faster_than_avg): 0.073
Info Gain (longest_slow_streak): 0.073
Info Gain (num_critical_overthinks): 0.039
Info Gain (burned_time): 0.063
Info Gain (opening_move): 0.35
    """