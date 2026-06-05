# pipeline/dags/daily_preprocess.py
from airflow.decorators import dag, task
from datetime import datetime
import polars as pl

@dag(schedule='0 2 * * *', start_date=datetime(2025, 1, 1))
def daily_preprocess():

    @task
    def extract() -> list:
        # PostgreSQL에서 어제 지출 데이터 가져오기
        ...
        return expenses

    @task
    def transform(expenses: list) -> dict:
        df = pl.DataFrame(expenses)
        summary = (
            df.group_by('category')
              .agg([
                  pl.col('amount').sum().alias('total'),
                  pl.col('amount').count().alias('count'),
                  pl.col('amount').mean().alias('avg')
              ])
              .sort('total', descending=True)
        )
        return summary.to_dict()

    @task
    def load(summary: dict):
        # 집계 결과 PostgreSQL에 저장
        # RAG 벡터DB 업데이트
        ...

    data = extract()
    summary = transform(data)
    load(summary)

daily_preprocess()