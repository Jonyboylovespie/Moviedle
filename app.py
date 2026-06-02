from flask import Flask, render_template, jsonify
import requests
import os
import random
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
TMDB_BASE_URL = "https://api.themoviedb.org/3"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/random-movie')
def random_movie():
    if not TMDB_API_KEY:
        return jsonify({"error": "TMDB_API_KEY not configured. Set it as an environment variable."}), 500
    
    random_page = random.randint(1, 10)
    
    url = f"{TMDB_BASE_URL}/discover/movie"
    params = {
        "api_key": TMDB_API_KEY,
        "sort_by": "popularity.desc",
        "page": random_page,
        "include_adult": "false",
        "language": "en-US"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            return jsonify({"error": "No movies found"}), 404
        
        movie = random.choice(results)
        return jsonify({
            "title": movie.get("title"),
            "overview": movie.get("overview"),
            "release_date": movie.get("release_date"),
            "poster_path": movie.get("poster_path")
        })
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
