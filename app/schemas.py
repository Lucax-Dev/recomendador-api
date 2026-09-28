from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                             max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Strategy = Literal["hybrid", "genres", "collaborative"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class User(Input):
    id: Identifier
    name: Text


class Genre(Input):
    name: Text


class Movie(Input):
    id: Identifier
    title: Text
    genres: list[Text] = Field(min_length=1, max_length=20)
    year: int | None = Field(None, ge=1888, le=2100)

    @field_validator("genres")
    @classmethod
    def unique_genres(cls, values):
        return sorted(set(values))


class Rating(Input):
    score: int = Field(ge=1, le=5, strict=True)


class Recommendation(BaseModel):
    id: str
    title: str
    genres: list[str]
    score: float
    genre_score: float
    similar_users: int
    average_rating: float
    watched_count: int
    reason: str


class Recommendations(BaseModel):
    user_id: str
    cache: Literal["HIT", "MISS", "BYPASS"]
    ttl_seconds: int
    strategy: Strategy
    recommendations: list[Recommendation]
