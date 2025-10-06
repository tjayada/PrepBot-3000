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
    for _, group in df.groupby(feature):
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


async def main():

    #import time
    #start_time = time.time()

    # check if df.csv exists
    import os
    if not os.path.isfile('df.csv'):
        print("df.csv not found. Fetching games from Lichess API...")

        token = "MY_TOKEN"
        #target = "Gogi78Sokol".lower()
        target = "tjayada".lower()
        color = "white"
        num = 50
        
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

    #ig_game_len, threshold = info_gain_numeric(df, 'game_length')
    ig_game_len = info_gain_categorical(df, 'game_length')
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


if __name__ == "__main__":
    asyncio.run(main())