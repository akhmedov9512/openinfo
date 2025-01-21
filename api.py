import asyncio
import logging
import sys
import uuid
import aiohttp
import sqlite3
import json
import time
from typing import Dict, List, Optional, Literal, Union
from datetime import datetime, timedelta, timezone
from pathlib import Path
import aiohttp
from io import BytesIO

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, UploadFile, File, Form
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready
from starlette.responses import JSONResponse
import uvicorn

from pydantic import BaseModel

import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is not set")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TOKEN}"

async def send_document_to_chat(
    chat_id: int,
    document: bytes,
    filename: str,
    caption: str
) -> bool:
    """Send document to telegram chat using pure API"""
    try:
        form_data = aiohttp.FormData()
        form_data.add_field(
            'document',
            document,
            filename=filename,
            content_type='application/octet-stream'
        )
        form_data.add_field('chat_id', str(chat_id))
        form_data.add_field('caption', caption)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TELEGRAM_API_URL}/sendDocument",
                data=form_data
            ) as response:
                if response.status == 429:  # Rate limit hit
                    retry_after = int((await response.json()).get('parameters', {}).get('retry_after', 60))
                    raise HTTPException(
                        status_code=429,
                        detail=f"Rate limit exceeded, retry after {retry_after} seconds"
                    )
                return response.status == 200
    except Exception as e:
        logging.error(f"Error sending document: {str(e)}")
        return False


celery_app = Celery('api')
celery_app.config_from_object('celeryconfig')

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot.db')
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS authorized_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username VARCHAR(255) NOT NULL,
                telegram_username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                last_login TIMESTAMP,
                last_logout TIMESTAMP,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                filename TEXT,
                content BLOB,
                description TEXT,
                created_at TIMESTAMP,
                status TEXT DEFAULT 'pending',
                total_users INTEGER,
                sent_count INTEGER DEFAULT 0,
                last_sent_at TIMESTAMP,
                next_batch_time TIMESTAMP,
                recipients_type TEXT,
                recipients_list TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS report_deliveries (
                delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT,
                user_id INTEGER,
                status TEXT,
                error_message TEXT,
                attempted_at TIMESTAMP,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY (report_id) REFERENCES reports(report_id),
                FOREIGN KEY (user_id) REFERENCES authorized_users(id)
            )
        ''')
        self.conn.commit()

    def get_users_batch(self, report_id: str, batch_size: int = 25) -> List[int]:
        cursor = self.conn.cursor()

        # Получаем тип получателей и список
        cursor.execute('''
            SELECT recipients_type, recipients_list
            FROM reports WHERE report_id = ?
        ''', (report_id,))
        report_data = cursor.fetchone()
        if not report_data:
            return []
            
        recipients_type, recipients_list = report_data
        
        if recipients_type == 'all':
            cursor.execute('''
                SELECT telegram_id 
                FROM authorized_users 
                WHERE is_active = TRUE 
                AND id NOT IN (
                    SELECT user_id FROM report_deliveries 
                    WHERE report_id = ? AND (status = 'delivered' OR retry_count >= 3)
                )
                LIMIT ?
            ''', (report_id, batch_size))
        else:
            usernames = json.loads(recipients_list)
            if not usernames:
                return []
                
            # Изменяем запрос для получения только существующих пользователей
            # без предварительной валидации
            placeholders = ','.join(['?' for _ in usernames])
            cursor.execute(f'''
                SELECT telegram_id 
                FROM authorized_users 
                WHERE is_active = TRUE 
                AND username IN ({placeholders})
                AND id NOT IN (
                    SELECT user_id FROM report_deliveries 
                    WHERE report_id = ? AND (status = 'delivered' OR retry_count >= 3)
                )
                LIMIT ?
            ''', (*usernames, report_id, batch_size))
        
        return [row[0] for row in cursor.fetchall()]
    
    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, telegram_id, username, is_active
            FROM authorized_users 
            WHERE telegram_id = ? AND is_active = TRUE
        ''', (telegram_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            'id': row[0],
            'telegram_id': row[1],
            'username': row[2],
            'is_active': row[3]
        }

    def update_report_progress(self, report_id: str, sent_count: int, next_batch_time: datetime):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE reports 
            SET sent_count = sent_count + ?, last_sent_at = ?, next_batch_time = ?
            WHERE report_id = ?
        ''', (sent_count, datetime.now(timezone.utc), next_batch_time, report_id))
        self.conn.commit()

    def get_report(self, report_id: str) -> Optional[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT report_id, filename, content, description, status, 
                   total_users, sent_count, last_sent_at, next_batch_time
            FROM reports WHERE report_id = ?
        ''', (report_id,))
        row = cursor.fetchone()
        if not row:
            return None
            
        return {
            'report_id': row[0],
            'filename': row[1],
            'content': row[2],
            'description': row[3],
            'status': row[4],
            'total_users': row[5],
            'sent_count': row[6],
            'last_sent_at': row[7],
            'next_batch_time': row[8]
        }

    def save_report(
        self, 
        filename: str, 
        content: bytes, 
        description: str,
        recipients: Dict[str, Union[str, List[str]]]
    ) -> str:
        report_id = str(uuid.uuid4())
        cursor = self.conn.cursor()
        
        # Get total number of users based on recipients type
        if recipients['type'] == 'all':
            cursor.execute('SELECT COUNT(*) FROM authorized_users WHERE is_active = TRUE')
            total_users = cursor.fetchone()[0]
        else:
            # Получаем только количество существующих пользователей
            usernames = recipients.get('usernames', [])
            if usernames:
                placeholders = ','.join(['?' for _ in usernames])
                cursor.execute(f'''
                    SELECT COUNT(*) FROM authorized_users 
                    WHERE is_active = TRUE AND username IN ({placeholders})
                ''', usernames)
                total_users = cursor.fetchone()[0]
            else:
                total_users = 0
        
        cursor.execute('''
            INSERT INTO reports (
                report_id, filename, content, description, created_at,
                status, total_users, next_batch_time,
                recipients_type, recipients_list
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            report_id, filename, content, description, 
            datetime.now(timezone.utc), 'pending', total_users,
            datetime.now(timezone.utc),
            recipients['type'],
            json.dumps(recipients.get('usernames', []))
        ))
        self.conn.commit()
        return report_id
    
    def mark_report_completed(self, report_id: str):
        cursor = self.conn.cursor()
        cursor.execute(
            'UPDATE reports SET status = ? WHERE report_id = ?',
            ('completed', report_id)
        )
        self.conn.commit()

    def log_delivery_attempt(
        self, 
        report_id: str, 
        telegram_id: int, 
        status: str, 
        error_message: Optional[str] = None
    ):
        cursor = self.conn.cursor()
        
        # Get user ID from telegram_id
        user = self.get_user_by_telegram_id(telegram_id)
        if not user:
            logging.error(f"User with telegram_id {telegram_id} not found")
            return
            
        # Get current retry count
        cursor.execute('''
            SELECT retry_count FROM report_deliveries 
            WHERE report_id = ? AND user_id = ?
        ''', (report_id, user['id']))
        row = cursor.fetchone()
        retry_count = (row[0] + 1) if row else 0
        
        cursor.execute('''
            INSERT OR REPLACE INTO report_deliveries 
            (report_id, user_id, status, error_message, attempted_at, retry_count)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            report_id, user['id'], status, error_message, 
            datetime.utcnow(), retry_count
        ))
        self.conn.commit()

    def get_report_status(self, report_id: str) -> Optional[Dict]:
        cursor = self.conn.cursor()
        
        # Get report details
        cursor.execute('''
            SELECT status, total_users, sent_count, created_at, last_sent_at 
            FROM reports WHERE report_id = ?
        ''', (report_id,))
        row = cursor.fetchone()
        if not row:
            return None
            
        # Get delivery statistics
        cursor.execute('''
            SELECT status, COUNT(*) 
            FROM report_deliveries 
            WHERE report_id = ?
            GROUP BY status
        ''', (report_id,))
        delivery_stats = dict(cursor.fetchall())
        
        return {
            'status': row[0],
            'total_users': row[1],
            'sent_count': row[2],
            'created_at': row[3],
            'last_sent_at': row[4],
            'delivery_stats': delivery_stats
        }

    def validate_usernames(self, usernames: List[str]) -> List[str]:
        cursor = self.conn.cursor()
        invalid_users = []
        
        for username in usernames:
            cursor.execute('''
                SELECT id FROM authorized_users 
                WHERE username = ? AND is_active = TRUE
            ''', (username,))
            if not cursor.fetchone():
                invalid_users.append(username)
                
        return invalid_users

    def cleanup_old_reports(self, days: int = 30):
        cleanup_date = datetime.utcnow() - timedelta(days=days)
        cursor = self.conn.cursor()
        
        # Get old report IDs
        cursor.execute(
            'SELECT report_id FROM reports WHERE created_at < ?',
            (cleanup_date,)
        )
        old_reports = cursor.fetchall()
        
        for (report_id,) in old_reports:
            # Delete related deliveries first
            cursor.execute(
                'DELETE FROM report_deliveries WHERE report_id = ?',
                (report_id,)
            )
            # Then delete the report
            cursor.execute(
                'DELETE FROM reports WHERE report_id = ?',
                (report_id,)
            )
            
        self.conn.commit()

    def get_reports_by_date_range(
        self,
        date_from: datetime,
        date_to: datetime,
        page: int = 1,
        per_page: int = 10
    ) -> Dict:
        cursor = self.conn.cursor()
        offset = (page - 1) * per_page
        
        # Get total count
        cursor.execute('''
            SELECT COUNT(*) FROM reports 
            WHERE created_at BETWEEN ? AND ?
        ''', (date_from, date_to))
        total = cursor.fetchone()[0]
        
        # Get reports
        cursor.execute('''
            SELECT report_id, filename, description, created_at, 
                status, recipients_type, recipients_list
            FROM reports
            WHERE created_at BETWEEN ? AND ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ''', (date_from, date_to, per_page, offset))
        
        reports = []
        for row in cursor.fetchall():
            reports.append({
                'report_id': row[0],
                'filename': row[1],
                'description': row[2],
                'created_at': row[3],
                'status': row[4],
                'recipients_type': row[5],
                'recipients_count': len(json.loads(row[6])) if row[5] == 'specific' else 'all'
            })
        
        return {
            'total': total,
            'page': page,
            'per_page': per_page,
            'reports': reports
        }



class Recipients(BaseModel):
    type: Literal["all", "specific"]
    usernames: Optional[List[str]] = None

class ReportRequest(BaseModel):
    description: str
    recipients: Dict[str, Union[str, List[str]]]

db = Database()
app = FastAPI()

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logging.error(f"Global error: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@celery_app.task(bind=True, max_retries=3)
def process_report_batch(self, report_id: str, retry: bool = False):
    try:
        report = db.get_report(report_id)
        if not report:
            return

        users_batch = db.get_users_batch(report_id)
        if not users_batch:
            db.mark_report_completed(report_id)
            return

        successful_sends = 0
        document_content = report['content']
        
        for user_id in users_batch:
            try:
                # Send message using pure Telegram API
                success = asyncio.run(send_document_to_chat(
                    chat_id=user_id,
                    document=document_content,
                    filename=report['filename'],
                    caption=report['description']
                ))
                
                if success:
                    db.log_delivery_attempt(report_id, user_id, 'delivered')
                    successful_sends += 1
                else:
                    db.log_delivery_attempt(
                        report_id, 
                        user_id, 
                        'failed',
                        "Failed to send document"
                    )
                
                # Rate limiting: 30 messages per second max
                time.sleep(0.035)  # ~28 messages per second
                
            except HTTPException as e:
                if e.status_code == 429:
                    retry_after = int(str(e.detail).split()[3])
                    time.sleep(retry_after)
                    process_report_batch.apply_async(
                        args=[report_id],
                        countdown=retry_after
                    )
                    return
                else:
                    db.log_delivery_attempt(
                        report_id, 
                        user_id, 
                        'failed',
                        str(e)
                    )
            except Exception as e:
                db.log_delivery_attempt(
                    report_id, 
                    user_id, 
                    'failed',
                    str(e)
                )
                logging.error(f"Failed to send to {user_id}: {str(e)}")

        next_batch_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        db.update_report_progress(report_id, successful_sends, next_batch_time)

        if len(users_batch) == successful_sends:
            process_report_batch.apply_async(
                args=[report_id],
                countdown=1
            )
        else:
            process_report_batch.apply_async(
                args=[report_id],
                countdown=60
            )

    except Exception as e:
        logging.error(f"Batch processing error: {str(e)}")
        self.retry(countdown=60, exc=e)

@celery_app.task
def cleanup_old_reports():
    db.cleanup_old_reports(days=30)

@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(
        crontab(hour=0, minute=0),
        cleanup_old_reports.s()
    )

@app.get("/api/v1/reports/{report_id}")
async def get_report_status(report_id: str):
    status = db.get_report_status(report_id)
    if not status:
        raise HTTPException(status_code=404, detail="Report not found")
    return status

@app.post("/api/v1/reports")
async def send_report(
    file: UploadFile = File(...),
    data: str = Form(...)
):
    try:
        request_data = json.loads(data)
        content = await file.read()
        
        report_id = db.save_report(
            filename=file.filename,
            content=content,
            description=request_data['description'],
            recipients=request_data['recipients']
        )
        
        process_report_batch.delay(report_id)
        return {"status": "accepted", "report_id": report_id}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in data field")

@app.get("/api/v1/reports")
async def get_reports(
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    page: int = Query(1, gt=0),
    per_page: int = Query(10, gt=0, le=100)
):
    reports = db.get_reports_by_date_range(date_from, date_to, page, per_page)
    return reports

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8001)
    finally:
        db.close()

