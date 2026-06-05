from models.schemas import ExpenseCreate
from models.mongo   import mongodb
from datetime       import datetime
from bson           import ObjectId

class ReceiptService:

    # ── 지출 저장 ──────────────────────────────────────
    async def save_expense(self, data: ExpenseCreate) -> str:
        doc = {
            **data.model_dump(),
            "date":       str(data.date),
            "created_at": datetime.utcnow().isoformat(),
        }
        res = await mongodb.expenses.insert_one(doc)
        return str(res.inserted_id)

    # ── 지출 목록 조회 ─────────────────────────────────
    async def get_expenses(
        self,
        year:  int,
        month: int,
        limit: int = 50
    ) -> list:
        query = {
            "date": {
                "$gte": f"{year}-{month:02d}-01",
                "$lte": f"{year}-{month:02d}-31",
            }
        }
        cursor = mongodb.expenses.find(query).sort("date", -1).limit(limit)
        expenses = []
        async for doc in cursor:
            expenses.append({
                "id":         str(doc["_id"]),
                "store_name": doc["store_name"],
                "amount":     doc["amount"],
                "date":       doc["date"],
                "category":   doc.get("category", "미분류"),
                "memo":       doc.get("memo"),
                "created_at": doc.get("created_at", ""),
            })
        return expenses

    # ── 지출 삭제 ──────────────────────────────────────
    async def delete_expense(self, expense_id: str) -> bool:
        res = await mongodb.expenses.delete_one({"_id": ObjectId(expense_id)})
        return res.deleted_count == 1

    # ── 월간 요약 ──────────────────────────────────────
    async def get_monthly_summary(self, year: int, month: int) -> dict:
        expenses = await self.get_expenses(year, month, limit=1000)

        total = sum(e["amount"] for e in expenses)

        by_category: dict = {}
        for e in expenses:
            cat = e["category"]
            by_category[cat] = by_category.get(cat, 0) + e["amount"]

        top_store = None
        if expenses:
            store_totals: dict = {}
            for e in expenses:
                store_totals[e["store_name"]] = store_totals.get(e["store_name"], 0) + e["amount"]
            top_store = max(store_totals, key=store_totals.get)

        return {
            "year":        year,
            "month":       month,
            "total":       total,
            "by_category": by_category,
            "top_store":   top_store,
        }

    # ── Phase 2에서 구현 예정 ──────────────────────────
    # async def process_image(self, image_bytes: bytes) -> dict:
    #     """OCR + LLM 파싱"""
    #     pass

    # async def classify_category(self, store_name: str, items: list) -> str:
    #     """카테고리 자동 분류"""
    #     pass

receipt_service = ReceiptService()