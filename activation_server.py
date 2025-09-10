#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏助手兑换码激活服务器
支持1天、7天、30天、90天、终生兑换码生成和验证
"""

import os
import sqlite3
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="游戏助手激活服务器", version="1.0.0")

# 数据库初始化
def init_database():
    """初始化数据库"""
    conn = sqlite3.connect('activation.db')
    cursor = conn.cursor()
    
    # 创建兑换码表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activation_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            license_type TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            is_used BOOLEAN DEFAULT FALSE,
            used_by_device TEXT,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            max_uses INTEGER DEFAULT 1,
            current_uses INTEGER DEFAULT 0,
            created_by TEXT DEFAULT 'system'
        )
    ''')
    
    # 创建设备激活记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            activation_code TEXT NOT NULL,
            license_type TEXT NOT NULL,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE,
            total_usage_days INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()

# 数据模型
class ActivationRequest(BaseModel):
    device_id: str
    activation_code: str

class ActivationResponse(BaseModel):
    success: bool
    message: str
    license_type: Optional[str] = None
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None

class VerificationRequest(BaseModel):
    device_id: str

class VerificationResponse(BaseModel):
    valid: bool
    license_type: Optional[str] = None
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None
    is_trial: bool = False

class GenerateCodeRequest(BaseModel):
    license_type: str  # TRIAL_1D, WEEK_7D, MONTH_1M, MONTH_3M, LIFETIME
    count: int = 1
    created_by: str = "admin"

# 工具函数
def generate_activation_code(license_type: str) -> str:
    """生成兑换码"""
    prefix_map = {
        "TRIAL_1D": "TRIAL_1D",
        "WEEK_7D": "WEEK_7D", 
        "MONTH_1M": "MONTH_1M",
        "MONTH_3M": "MONTH_3M",
        "LIFETIME": "LIFETIME"
    }
    
    prefix = prefix_map.get(license_type, "UNKNOWN")
    random_part = secrets.token_hex(4).upper()
    return f"{prefix}_{random_part}"

def get_duration_days(license_type: str) -> int:
    """获取授权天数"""
    duration_map = {
        "TRIAL_1D": 1,
        "WEEK_7D": 7,
        "MONTH_1M": 30,
        "MONTH_3M": 90,
        "LIFETIME": 36500  # 100年，相当于终生
    }
    return duration_map.get(license_type, 0)

def calculate_expiry_date(duration_days: int) -> datetime:
    """计算过期时间"""
    if duration_days >= 36500:  # 终生
        return datetime.now() + timedelta(days=36500)
    return datetime.now() + timedelta(days=duration_days)

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect('activation.db')
    conn.row_factory = sqlite3.Row
    return conn

# API端点
@app.post("/api/activate", response_model=ActivationResponse)
async def activate_device(request: ActivationRequest):
    """激活设备"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 验证兑换码格式
        if not request.activation_code.startswith(('TRIAL_1D_', 'WEEK_7D_', 'MONTH_1M_', 'MONTH_3M_', 'LIFETIME_')):
            return ActivationResponse(
                success=False,
                message="兑换码格式错误"
            )
        
        # 查询兑换码
        cursor.execute('''
            SELECT * FROM activation_codes 
            WHERE code = ? AND is_used = FALSE
        ''', (request.activation_code,))
        
        code_record = cursor.fetchone()
        if not code_record:
            return ActivationResponse(
                success=False,
                message="兑换码不存在或已被使用"
            )
        
        # 检查是否过期
        if code_record['expires_at'] and datetime.fromisoformat(code_record['expires_at']) < datetime.now():
            return ActivationResponse(
                success=False,
                message="兑换码已过期"
            )
        
        # 检查使用次数
        if code_record['current_uses'] >= code_record['max_uses']:
            return ActivationResponse(
                success=False,
                message="兑换码使用次数已达上限"
            )
        
        # 检查设备是否已激活
        cursor.execute('''
            SELECT * FROM device_activations 
            WHERE device_id = ? AND is_active = TRUE
        ''', (request.device_id,))
        
        existing_device = cursor.fetchone()
        if existing_device:
            return ActivationResponse(
                success=False,
                message="该设备已激活，请勿重复激活"
            )
        
        # 计算过期时间
        duration_days = code_record['duration_days']
        expires_at = calculate_expiry_date(duration_days)
        
        # 更新兑换码使用状态
        cursor.execute('''
            UPDATE activation_codes 
            SET is_used = TRUE, used_by_device = ?, used_at = CURRENT_TIMESTAMP,
                current_uses = current_uses + 1
            WHERE code = ?
        ''', (request.device_id, request.activation_code))
        
        # 创建设备激活记录
        cursor.execute('''
            INSERT INTO device_activations 
            (device_id, activation_code, license_type, expires_at)
            VALUES (?, ?, ?, ?)
        ''', (request.device_id, request.activation_code, 
              code_record['license_type'], expires_at.isoformat()))
        
        conn.commit()
        
        return ActivationResponse(
            success=True,
            message="激活成功",
            license_type=code_record['license_type'],
            expires_at=expires_at.isoformat(),
            days_remaining=duration_days
        )
        
    except Exception as e:
        conn.rollback()
        return ActivationResponse(
            success=False,
            message=f"激活失败: {str(e)}"
        )
    finally:
        conn.close()

@app.post("/api/verify", response_model=VerificationResponse)
async def verify_device(request: VerificationRequest):
    """验证设备激活状态"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 查询设备激活状态
        cursor.execute('''
            SELECT * FROM device_activations 
            WHERE device_id = ? AND is_active = TRUE
        ''', (request.device_id,))
        
        device_record = cursor.fetchone()
        if not device_record:
            return VerificationResponse(
                valid=False,
                is_trial=False
            )
        
        # 检查是否过期
        expires_at = datetime.fromisoformat(device_record['expires_at'])
        now = datetime.now()
        
        if expires_at < now:
            # 过期，更新状态
            cursor.execute('''
                UPDATE device_activations 
                SET is_active = FALSE 
                WHERE device_id = ?
            ''', (request.device_id,))
            conn.commit()
            
            return VerificationResponse(
                valid=False,
                is_trial=False
            )
        
        # 更新最后访问时间
        cursor.execute('''
            UPDATE device_activations 
            SET last_seen = CURRENT_TIMESTAMP 
            WHERE device_id = ?
        ''', (request.device_id,))
        conn.commit()
        
        # 计算剩余天数
        days_remaining = (expires_at - now).days
        is_trial = device_record['license_type'] == 'TRIAL_1D'
        
        return VerificationResponse(
            valid=True,
            license_type=device_record['license_type'],
            expires_at=device_record['expires_at'],
            days_remaining=days_remaining,
            is_trial=is_trial
        )
        
    except Exception as e:
        return VerificationResponse(
            valid=False,
            is_trial=False
        )
    finally:
        conn.close()

@app.post("/api/admin/generate-codes")
async def generate_codes(request: GenerateCodeRequest):
    """生成兑换码（管理员功能）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        duration_days = get_duration_days(request.license_type)
        if duration_days == 0:
            raise HTTPException(status_code=400, detail="无效的授权类型")
        
        codes = []
        for _ in range(request.count):
            code = generate_activation_code(request.license_type)
            expires_at = calculate_expiry_date(duration_days)
            
            cursor.execute('''
                INSERT INTO activation_codes 
                (code, license_type, duration_days, expires_at, created_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (code, request.license_type, duration_days, 
                  expires_at.isoformat(), request.created_by))
            
            codes.append(code)
        
        conn.commit()
        
        return {
            "success": True,
            "message": f"成功生成 {len(codes)} 个兑换码",
            "codes": codes,
            "license_type": request.license_type,
            "duration_days": duration_days
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/admin/stats")
async def get_statistics():
    """获取统计信息（管理员功能）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 兑换码统计
        cursor.execute('''
            SELECT license_type, 
                   COUNT(*) as total,
                   SUM(CASE WHEN is_used = TRUE THEN 1 ELSE 0 END) as used,
                   SUM(CASE WHEN is_used = FALSE THEN 1 ELSE 0 END) as unused
            FROM activation_codes 
            GROUP BY license_type
        ''')
        code_stats = cursor.fetchall()
        
        # 设备激活统计
        cursor.execute('''
            SELECT license_type,
                   COUNT(*) as total_devices,
                   SUM(CASE WHEN is_active = TRUE THEN 1 ELSE 0 END) as active_devices
            FROM device_activations 
            GROUP BY license_type
        ''')
        device_stats = cursor.fetchall()
        
        return {
            "code_statistics": [dict(row) for row in code_stats],
            "device_statistics": [dict(row) for row in device_stats]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/")
async def root():
    """根路径"""
    return {"message": "游戏助手激活服务器运行中", "version": "1.0.0"}

if __name__ == "__main__":
    # 初始化数据库
    init_database()
    
    # 启动服务器
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        log_level="info"
    )
