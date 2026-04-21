"""
auth.py  —  JWT Authentication Module for FontScan
"""

from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel

# ── Config ───────────────────────────────────────────────────
SECRET_KEY                  = "fontscan-ey-secret-change-in-production-2024"
ALGORITHM                   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60          # 1 hour

# ── Database ─────────────────────────────────────────────────
DATABASE_URL = "sqlite:///./fontscan_users.db"
engine       = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()


# ── ORM Model ────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String, unique=True, index=True, nullable=False)
    email           = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role            = Column(String, default="user")   # "admin" | "user"


Base.metadata.create_all(bind=engine)


# ── Pydantic Schemas ─────────────────────────────────────────
class UserCreate(BaseModel):
    username: str
    email:    str
    password: str
    role:     str = "user"


class UserLogin(BaseModel):
    email:    str
    password: str


class Token(BaseModel):
    access_token: str
    token_type:   str


class UserOut(BaseModel):
    id:       int
    username: str
    email:    str
    role:     str

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────
pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire    = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise exc
    except JWTError:
        raise exc
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise exc
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


def verify_token_param(token: str) -> User:
    """Verify JWT passed as query param (for EventSource which can't set headers)."""
    exc = HTTPException(status_code=401, detail="Invalid or expired token.")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise exc
    except JWTError:
        raise exc
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise exc
        return user
    finally:
        db.close()


# ── Seed default admin ────────────────────────────────────────
def init_default_admin():
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                username        = "admin",
                email           = "admin@fontscan.com",
                hashed_password = hash_password("admin123"),
                role            = "admin",
            )
            db.add(admin)
            db.commit()
            print("  ✓ Default admin created → admin@fontscan.com / admin123")
            print("  ⚠  Change this password after first login!")
    finally:
        db.close()
        