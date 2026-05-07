from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    Integer, Numeric, SmallInteger, String, Text, ARRAY, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(128), nullable=True)
    first_name = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    profile = relationship("Profile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    preferences = relationship("Preferences", back_populates="user", uselist=False, cascade="all, delete-orphan")
    interactions_made = relationship("Interaction", foreign_keys="Interaction.actor_user_id", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username={self.username})>"


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    bio = Column(Text, nullable=True)
    interests = Column(JSONB, nullable=True)
    photo_urls = Column(ARRAY(String), nullable=True, default=list)
    completeness_score = Column(Numeric(5, 4), default=0)
    age = Column(SmallInteger, nullable=True)
    gender = Column(String(20), nullable=True)
    city = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="profile")
    rating = relationship("Rating", back_populates="profile", uselist=False, cascade="all, delete-orphan")
    interactions_received = relationship("Interaction", foreign_keys="Interaction.target_profile_id", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("age >= 18", name="check_age_minimum"),
        Index("ix_profiles_city", "city"),
        Index("ix_profiles_gender_age", "gender", "age"),
    )

    def __repr__(self):
        return f"<Profile(id={self.id}, user_id={self.user_id}, city={self.city})>"


class Preferences(Base):
    __tablename__ = "preferences"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    age_min = Column(SmallInteger, default=18)
    age_max = Column(SmallInteger, default=99)
    gender_pref = Column(String(20), nullable=True)
    city_pref = Column(String(100), nullable=True)

    user = relationship("User", back_populates="preferences")

    def __repr__(self):
        return f"<Preferences(id={self.id}, user_id={self.user_id}, age_min={self.age_min}, age_max={self.age_max})>"


class Rating(Base):
    __tablename__ = "ratings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    profile_id = Column(BigInteger, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, unique=True)
    primary_score = Column(Numeric(6, 2), default=0)
    behavioral_score = Column(Numeric(6, 2), default=0)
    combined_score = Column(Numeric(6, 2), default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    profile = relationship("Profile", back_populates="rating")

    __table_args__ = (
        Index("ix_ratings_combined_score", "combined_score"),
    )

    def __repr__(self):
        return f"<Rating(id={self.id}, profile_id={self.profile_id}, combined_score={self.combined_score})>"


class Interaction(Base):
    __tablename__ = "interactions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_profile_id = Column(BigInteger, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(String(20), nullable=False)
    is_match = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("action IN ('like', 'pass', 'super_like')", name="check_action_valid"),
        Index("ix_interactions_target_action", "target_profile_id", "action"),
        Index("ix_interactions_actor_target", "actor_user_id", "target_profile_id"),
        Index("ix_interactions_target_match", "target_profile_id", "is_match"),
    )

    def __repr__(self):
        return f"<Interaction(id={self.id}, actor={self.actor_user_id}, target={self.target_profile_id}, action={self.action})>"
