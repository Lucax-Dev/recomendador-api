"""Carga idempotente de dados sintéticos; nunca apaga o banco."""
import argparse
import json
from pathlib import Path
import httpx


def seed(base_url):
    data = json.loads((Path(__file__).resolve().parents[1] / "data/demo.json").read_text())
    with httpx.Client(base_url=base_url, timeout=60, trust_env=False) as client:
        def send(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
        for name in data["genres"]:
            send("POST", "/genres", json={"name":name})
        for user in data["users"]:
            send("POST", "/users", json=user)
        for movie in data["movies"]:
            send("POST", "/movies", json=movie)
        for item in data["ratings"]:
            send("PUT", f"/users/{item['user_id']}/ratings/{item['movie_id']}", json={"score":item["score"]})
        for item in data["watched"]:
            send("POST", f"/users/{item['user_id']}/watched/{item['movie_id']}")
    print(f"Carga concluída: {len(data['users'])} usuários, {len(data['movies'])} filmes, {len(data['genres'])} gêneros, {len(data['ratings'])} avaliações.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    seed(parser.parse_args().base_url)
