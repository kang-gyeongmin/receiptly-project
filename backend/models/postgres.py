from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase): pass

class Expense(Base):
    __tablename__ = 'expenses'

    id          = Column(Integer, primary_key=True)
    user_id     = Column(String)
    store_name  = Column(String)
    category    = Column(String)
    amount      = Column(Integer)
    date        = Column(Date)
    mongo_id    = Column(String)   # MongoDB 원본 참조
    created_at  = Column(DateTime, default=datetime.utcnow)