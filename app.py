import os
import time
import threading
import traceback
import pytz
import datetime

from flask import (
    Flask, request, render_template, redirect,
    url_for, flash, session, g
)
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, Integer, String,
    Boolean, ForeignKey, func, or_
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from sqlalchemy.exc import SQLAlchemyError

# ─────────────────────────────────────────────────────────
# 환경변수 로드
# ─────────────────────────────────────────────────────────
load_dotenv()
ADMIN_ID   = os.getenv("ADMIN_ID", "admin")
ADMIN_PW   = os.getenv("ADMIN_PW", "admin123")
KITCHEN_ID = os.getenv("KITCHEN_ID", "kitchen")
KITCHEN_PW = os.getenv("KITCHEN_PW", "kitchen123")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS", "A1b2C3d4!")

DB_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"

# ─────────────────────────────────────────────────────────
# Flask, SQLAlchemy 초기화
# ─────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.secret_key = os.urandom(24)

engine = create_engine(
    DB_URL,
    pool_size=10, max_overflow=5,
    pool_timeout=30, pool_recycle=1800,
    echo=False
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

def get_db_session():
    if "db_session" not in g:
        g.db_session = SessionLocal()
    return g.db_session

@app.teardown_appcontext
def teardown_db(exception=None):
    db = g.pop("db_session", None)
    if db:
        db.close()

# ─────────────────────────────────────────────────────────
# 한국 시간(KST) HHMMSS 포맷 유틸
# ─────────────────────────────────────────────────────────
def current_hhmmss():
    """한국시간(Asia/Seoul)을 HHMMSS(예: '221305') 형태 문자열로 반환"""
    now = datetime.datetime.now(pytz.timezone('Asia/Seoul'))
    return now.strftime("%H%M%S")

def hhmmss_to_minutes(hhmmss_str):
    """
    HHMMSS 형태의 문자열을 분 단위로 환산하여 정수로 반환.
    하루 기준(자정~자정)으로만 사용한다.
    """
    hh = int(hhmmss_str[0:2])
    mm = int(hhmmss_str[2:4])
    ss = int(hhmmss_str[4:6])
    total_seconds = hh * 3600 + mm * 60 + ss
    return total_seconds // 60

# ─────────────────────────────────────────────────────────
# 전역 딕셔너리(테이블별 설정/사용상태/차단 등)
# DB에 저장하지 않고 메모리에서 관리(지속성 없음)
# ─────────────────────────────────────────────────────────
# N명당 M개의 메인안주 로직을 위해 main_anju_people, main_anju_count 추가
_table_settings_state = {}  # tableNumber -> (mainRequired, mainAnjuPeople, mainAnjuCount, beerLimitEn, beerLimitCnt)
_table_usage_start    = {}  # tableNumber -> confirmedAt(HHMMSS)
_blocked_tables       = set()  # 차단된 테이블 관리

# ─────────────────────────────────────────────────────────
# 모델 정의
# ─────────────────────────────────────────────────────────
class Menu(Base):
    __tablename__ = "menu"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    name     = Column(String(100), nullable=False)
    price    = Column(Integer, nullable=False)
    category = Column(String(50), nullable=False)
    stock    = Column(Integer, nullable=False, default=0)
    sold_out = Column(Boolean, default=False)

class Order(Base):
    __tablename__ = "orders"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    order_id      = Column(String(50), nullable=False)    
    tableNumber   = Column(String(50), nullable=False)
    peopleCount   = Column(Integer, nullable=False)
    totalPrice    = Column(Integer, nullable=False)
    status        = Column(String(20), nullable=False)
    createdAt     = Column(Integer, nullable=False)   # HHMMSS(정수)
    confirmedAt   = Column(Integer)                   # HHMMSS(정수)
    alertTime1    = Column(Integer, default=0)        # 0 또는 1
    alertTime2    = Column(Integer, default=0)        # 0 또는 1
    service       = Column(Boolean, default=False)
    items         = relationship("OrderItem", back_populates="order")

class OrderItem(Base):
    __tablename__ = "order_items"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    order_id         = Column(Integer, ForeignKey("orders.id"), nullable=False)
    menu_id          = Column(Integer, ForeignKey("menu.id"), nullable=False)
    quantity         = Column(Integer, nullable=False)
    doneQuantity     = Column(Integer, default=0)
    deliveredQuantity= Column(Integer, default=0)
    order            = relationship("Order", back_populates="items")
    menu             = relationship("Menu")

class Log(Base):
    __tablename__ = "logs"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    time    = Column(Integer, nullable=False)   # HHMMSS(정수)
    role    = Column(String(50), nullable=False)
    action  = Column(String(50), nullable=False)
    detail  = Column(String(200), nullable=False)

class Setting(Base):
    __tablename__ = "settings"
    id                    = Column(Integer, primary_key=True)
    main_required_enabled = Column(Boolean, default=True)
    main_anju_people      = Column(Integer, default=3)  # N명당
    main_anju_count       = Column(Integer, default=1)  # M개
    beer_limit_enabled    = Column(Boolean, default=True)
    beer_limit_count      = Column(Integer, default=1)
    time_warning1         = Column(Integer, default=50)
    time_warning2         = Column(Integer, default=60)
    total_tables          = Column(Integer, default=12)

# ─────────────────────────────────────────────────────────
# DB 초기화 & 기본데이터 삽입
# ─────────────────────────────────────────────────────────
def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Menu).count() == 0:
            sample = [
                Menu(name="메인안주A", price=12000, category="main", stock=10),
                Menu(name="메인안주B", price=15000, category="main", stock=7),
                Menu(name="소주",      price=4000,  category="soju",  stock=100),
                Menu(name="맥주(1L)",  price=8000,  category="beer",  stock=50),
                Menu(name="콜라",      price=2000,  category="drink", stock=40),
                Menu(name="사이다",    price=2000,  category="drink", stock=40),
                Menu(name="생수",      price=1000,  category="drink", stock=50),
            ]
            db.add_all(sample)
        if not db.query(Setting).filter_by(id=1).first():
            db.add(Setting(
                id=1,
                main_required_enabled=True,
                main_anju_people=3,
                main_anju_count=1,
                beer_limit_enabled=True,
                beer_limit_count=1,
                time_warning1=50,
                time_warning2=60,
                total_tables=12
            ))
        db.commit()
    finally:
        db.close()

def log_action(role, action, detail):
    db = get_db_session()
    now_int = int(current_hhmmss())
    db.add(Log(time=now_int, role=role, action=action, detail=detail))
    db.commit()

def get_settings():
    db = get_db_session()
    s = db.query(Setting).filter_by(id=1).first()
    if not s:
        s = Setting(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s

def update_settings(form):
    db = get_db_session()
    s = db.query(Setting).filter_by(id=1).first()
    s.main_required_enabled = (form.get("mainRequiredEnabled")=="1")
    s.main_anju_people      = int(form.get("mainAnjuPeople","3") or 3)  # N
    s.main_anju_count       = int(form.get("mainAnjuCount","1") or 1)   # M
    s.beer_limit_enabled    = (form.get("beerLimitEnabled")=="1")
    s.beer_limit_count      = int(form.get("beerLimitCount","1") or 1)
    s.time_warning1         = int(form.get("timeWarning1","50") or 50)
    s.time_warning2         = int(form.get("timeWarning2","60") or 60)
    s.total_tables          = int(form.get("totalTables","12") or 12)
    db.commit()

# ─────────────────────────────────────────────────────────
# 50/60분 체크 쓰레드
# ─────────────────────────────────────────────────────────
_started = False
def start_time_checker():
    global _started
    if _started:
        return
    _started = True

    def runner():
        while True:
            time.sleep(60)
            try:
                db = SessionLocal()
                s = db.query(Setting).filter_by(id=1).first()
                now_str = current_hhmmss()
                now_min = hhmmss_to_minutes(now_str)

                orders = db.query(Order).filter(Order.status=="paid", Order.confirmedAt!=None).all()
                for o in orders:
                    c_str = f"{o.confirmedAt:06d}"
                    confirm_min = hhmmss_to_minutes(c_str)
                    diff = now_min - confirm_min

                    if o.alertTime1==0 and diff>=s.time_warning1:
                        o.alertTime1=1
                        db.commit()
                        db.add(Log(time=int(current_hhmmss()), role="system", action="TIME_WARNING1", detail=f"id={o.order_id}"))
                        db.commit()
                    if o.alertTime2==0 and diff>=s.time_warning2:
                        o.alertTime2=1
                        db.commit()
                        db.add(Log(time=int(current_hhmmss()), role="system", action="TIME_WARNING2", detail=f"id={o.order_id}"))
                        db.commit()

                db.close()
            except Exception:
                traceback.print_exc()
    threading.Thread(target=runner, daemon=True).start()

# ─────────────────────────────────────────────────────────
# 에러 핸들러
# ─────────────────────────────────────────────────────────
@app.errorhandler(SQLAlchemyError)
def handle_db_error(e):
    traceback.print_exc()
    flash(f"DB 오류: {e}", "error")
    return redirect(url_for("index"))

# ─────────────────────────────────────────────────────────
# 로그인 / 로그아웃
# ─────────────────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        uid = request.form.get("userid")
        pw  = request.form.get("userpw")
        if uid==ADMIN_ID and pw==ADMIN_PW:
            session["role"] = "admin"
            flash("관리자 로그인되었습니다.")
            return redirect(url_for("admin"))
        elif uid==KITCHEN_ID and pw==KITCHEN_PW:
            session["role"] = "kitchen"
            flash("주방 로그인되었습니다.")
            return redirect(url_for("kitchen"))
        else:
            flash("로그인 실패: 아이디/비밀번호를 확인하세요.")
            return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("role", None)
    flash("로그아웃 되었습니다.")
    return redirect(url_for("index"))

def login_required(fn):
    def wrapper(*args, **kwargs):
        if "role" not in session:
            flash("로그인 후 이용 가능합니다.")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper

# ─────────────────────────────────────────────────────────
# 메인 페이지
# ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

# ─────────────────────────────────────────────────────────
# 테이블 empty / block
# ─────────────────────────────────────────────────────────
@app.route("/admin/empty_table/<table_num>", methods=["POST"])
@login_required
def admin_empty_table(table_num):
    if table_num in _table_settings_state:
        del _table_settings_state[table_num]
    if table_num in _table_usage_start:
        del _table_usage_start[table_num]
    flash(f"{table_num}번 테이블이(가) 비워졌습니다.")
    log_action(session["role"], "EMPTY_TABLE", f"table={table_num}")
    return redirect(url_for("admin"))

@app.route("/admin/block_table/<table_num>", methods=["POST"])
@login_required
def admin_block_table(table_num):
    if table_num in _blocked_tables:
        _blocked_tables.remove(table_num)
        flash(f"{table_num}번 테이블의 차단이 해제되었습니다.")
        log_action(session["role"], "UNBLOCK_TABLE", f"table={table_num}")
    else:
        _blocked_tables.add(table_num)
        flash(f"{table_num}번 테이블을 차단했습니다.")
        log_action(session["role"], "BLOCK_TABLE", f"table={table_num}")
    return redirect(url_for("admin"))

# ─────────────────────────────────────────────────────────
# 주문하기
# ─────────────────────────────────────────────────────────
@app.route("/order", methods=["GET","POST"])
def order():
    db = get_db_session()
    menu_list = db.query(Menu).all()
    settings  = get_settings()

    table_numbers = ["TAKEOUT"] + [str(i) for i in range(1, settings.total_tables+1)]

    if request.method=="POST":
        table_number   = request.form.get("tableNumber","")
        is_first_order = (request.form.get("isFirstOrder")=="true")
        people_count   = int(request.form.get("peopleCount","0") or 0)
        notice_checked = (request.form.get("noticeChecked")=="on")

        if table_number in _blocked_tables:
            flash("현재 차단된 테이블입니다. 주문 불가합니다.", "error")
            return redirect(url_for("order"))

        ordered_items = []
        for m in menu_list:
            qty_str = request.form.get(f"qty_{m.id}", "0")
            qty = int(qty_str or 0)
            if qty > 0:
                if m.sold_out:
                    flash(f"품절된 메뉴[{m.name}]는 주문 불가합니다.", "error")
                    return redirect(url_for("order"))
                ordered_items.append((m, qty))

        if not table_number:
            flash("테이블 번호를 선택해주세요.", "error")
            return redirect(url_for("order"))

        if is_first_order:
            if not notice_checked:
                flash("최초 주문 시 주의사항 확인이 필수입니다.", "error")
                return redirect(url_for("order"))
            if people_count < 1:
                flash("최초 주문 시 인원수는 1명 이상이어야 합니다.", "error")
                return redirect(url_for("order"))

            # snapshot (mainReq, mainAnjuPeople, mainAnjuCount, beerLimitEn, beerLimitCnt)
            if table_number not in _table_settings_state:
                _table_settings_state[table_number] = (
                    settings.main_required_enabled,
                    settings.main_anju_people,
                    settings.main_anju_count,
                    settings.beer_limit_enabled,
                    settings.beer_limit_count
                )
            mainReq, anjuPeople, anjuCount, beerLimitEn, beerLimitCnt = _table_settings_state[table_number]

            if mainReq:
                # N명당 M개 => needed = (people_count // N)*M
                needed = (people_count // anjuPeople) * anjuCount
                main_qty = sum(q for (menu_obj, q) in ordered_items if menu_obj.category=="main")
                if needed>0 and main_qty<needed:
                    flash(f"인원수 대비 메인안주가 부족합니다. (필요: {needed}개)", "error")
                    return redirect(url_for("order"))

            if beerLimitEn:
                beer_count = sum(q for (menu_obj, q) in ordered_items if menu_obj.category=="beer")
                if beer_count > beerLimitCnt:
                    flash(f"최초 주문 시 맥주는 최대 {beerLimitCnt}병까지 가능합니다.", "error")
                    return redirect(url_for("order"))
        else:
            # 추가 주문
            if table_number not in _table_settings_state:
                # 만약 empty 등으로 snapshot이 사라졌다면 현재 설정을 스냅샷으로
                _table_settings_state[table_number] = (
                    settings.main_required_enabled,
                    settings.main_anju_people,
                    settings.main_anju_count,
                    settings.beer_limit_enabled,
                    settings.beer_limit_count
                )
            mainReq, anjuPeople, anjuCount, beerLimitEn, beerLimitCnt = _table_settings_state[table_number]

            if beerLimitEn:
                for (menu_obj, q) in ordered_items:
                    if menu_obj.category=="beer" and q>0:
                        flash("추가 주문에서는 맥주를 주문할 수 없습니다.", "error")
                        return redirect(url_for("order"))

        new_order_id_str = current_hhmmss()
        total_price = sum(m.price*q for (m,q) in ordered_items)
        now_int = int(current_hhmmss())

        try:
            new_order = Order(
                order_id=new_order_id_str,
                tableNumber=table_number,
                peopleCount=people_count,
                totalPrice=total_price,
                status="pending",
                createdAt=now_int,
                confirmedAt=None,
                alertTime1=0,
                alertTime2=0,
                service=False
            )
            db.add(new_order)
            db.flush()

            for (m,q) in ordered_items:
                oi = OrderItem(order_id=new_order.id, menu_id=m.id,
                               quantity=q, doneQuantity=0, deliveredQuantity=0)
                db.add(oi)

            db.commit()
            flash(f"주문이 접수되었습니다 (주문번호: {new_order_id_str}).")
            return render_template("order_result.html",
                                   total_price=total_price,
                                   order_id=new_order_id_str)
        except:
            db.rollback()
            flash("주문 처리 중 오류가 발생했습니다.", "error")
            return redirect(url_for("order"))

    return render_template("order_form.html",
                           menu_items=menu_list,
                           table_numbers=table_numbers,
                           settings=settings)

# ─────────────────────────────────────────────────────────
# 관리자 페이지
# ─────────────────────────────────────────────────────────
@app.route("/admin")
@login_required
def admin():
    db = get_db_session()

    sort_mode = request.args.get("sort","asc")
    if sort_mode not in ["asc","desc"]:
        sort_mode = "asc"

    s = get_settings()
    total_tables = s.total_tables

    table_list = ["TAKEOUT"] + [str(i) for i in range(1, total_tables+1)]

    now_str = current_hhmmss()
    now_min = hhmmss_to_minutes(now_str)

    table_status_info = []
    for t in table_list:
        blocked = (t in _blocked_tables)
        if t in _table_usage_start:
            c_str = f"{_table_usage_start[t]:06d}"
            c_min = hhmmss_to_minutes(c_str)
            diff = now_min - c_min
            color = ""
            if diff >= s.time_warning2:
                color = "red"
            elif diff >= s.time_warning1:
                color = "yellow"
            else:
                color = "normal"
            table_status_info.append((t, f"{diff}분", color, blocked, False))
        else:
            table_status_info.append((t, "-", "empty", blocked, True))

    if sort_mode=="asc":
        order_by_clause = Order.id.asc()
    else:
        order_by_clause = Order.id.desc()

    # pending
    pending_orders = []
    pending_q = db.query(Order).filter_by(status="pending").order_by(order_by_clause)
    for o in pending_q:
        items = [{"menuName": it.menu.name, "quantity": it.quantity} for it in o.items]
        # 최초 주문?
        is_first = (o.peopleCount > 0)
        # 최근 최초 주문 시각
        recent_first_order = db.query(Order).filter(Order.tableNumber==o.tableNumber, Order.peopleCount>0).order_by(Order.id.desc()).first()
        recent_first_time  = recent_first_order.order_id if recent_first_order else ""
        stock_negative_warning = any(it.menu.stock<0 for it in o.items)

        pending_orders.append({
            "id": o.id,
            "order_id": o.order_id,
            "tableNumber": o.tableNumber,
            "peopleCount": o.peopleCount,
            "totalPrice": o.totalPrice,
            "items": items,
            "is_first": is_first,
            "recent_first_time": recent_first_time,
            "stock_negative_warning": stock_negative_warning,
            "createdAt": o.createdAt
        })

    # paid
    paid_orders = []
    paid_q = db.query(Order).filter_by(status="paid").order_by(order_by_clause)
    for o in paid_q:
        items = []
        for it in o.items:
            items.append({
                "menuName": it.menu.name,
                "quantity": it.quantity,
                "doneQuantity": it.doneQuantity,
                "deliveredQuantity": it.deliveredQuantity
            })
        paid_orders.append({
            "id": o.id,
            "order_id": o.order_id,
            "tableNumber": o.tableNumber,
            "totalPrice": o.totalPrice,
            "items": items,
            "service": o.service,
            "createdAt": o.createdAt
        })

    # completed
    completed_orders = []
    completed_q = db.query(Order).filter_by(status="completed").order_by(order_by_clause)
    for o in completed_q:
        completed_orders.append({
            "id": o.id,
            "order_id": o.order_id,
            "tableNumber": o.tableNumber,
            "totalPrice": o.totalPrice,
            "service": o.service,
            "createdAt": o.createdAt
        })

    # rejected
    rejected_orders = []
    rejected_q = db.query(Order).filter_by(status="rejected").order_by(order_by_clause)
    for o in rejected_q:
        rejected_orders.append({
            "id": o.id,
            "order_id": o.order_id,
            "tableNumber": o.tableNumber,
            "createdAt": o.createdAt
        })

    menu_items = [{
        "id": m.id,
        "name": m.name,
        "price": m.price,
        "category": m.category,
        "stock": m.stock,
        "soldOut": m.sold_out
    } for m in db.query(Menu).all()]

    sales_sum = db.query(
        func.coalesce(func.sum(Order.totalPrice), 0)
    ).filter(
        Order.service == False,
        or_(Order.status == "paid", Order.status == "completed")
    ).scalar()

    return render_template("admin.html",
        table_status_info=table_status_info,
        pending_orders=pending_orders,
        paid_orders=paid_orders,
        completed_orders=completed_orders,
        rejected_orders=rejected_orders,
        menu_items=menu_items,
        current_sales=sales_sum,
        settings=s,
        sort_mode=sort_mode
    )

@app.route("/admin/confirm/<int:order_id>", methods=["POST"])
@login_required
def admin_confirm(order_id):
    db = get_db_session()
    now_int = int(current_hhmmss())
    try:
        o = db.query(Order).filter_by(id=order_id).with_for_update().first()
        if not o or o.status!="pending":
            flash("해당 주문은 'pending' 상태가 아닙니다.")
            return redirect(url_for("admin"))

        o.status = "paid"
        o.confirmedAt = now_int

        if o.peopleCount>0 and o.tableNumber not in _table_usage_start:
            _table_usage_start[o.tableNumber] = o.confirmedAt

        for it in o.items:
            m = db.query(Menu).filter_by(id=it.menu_id).with_for_update().first()
            m.stock -= it.quantity
            # 음수 허용
            if m.category in ["soju","beer","drink"]:
                it.doneQuantity = it.quantity

        db.commit()
        log_action(session["role"], "CONFIRM_ORDER", f"주문ID={order_id}")
        flash(f"주문 {order_id} 입금확인 완료!")
    except:
        db.rollback()
        flash("입금 확인 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

@app.route("/admin/reject/<int:order_id>", methods=["POST"])
@login_required
def admin_reject(order_id):
    db = get_db_session()
    try:
        o = db.query(Order).filter_by(id=order_id).with_for_update().first()
        if not o or o.status!="pending":
            flash("해당 주문은 'pending' 상태가 아닙니다.")
            return redirect(url_for("admin"))
        o.status = "rejected"
        db.commit()
        log_action(session["role"], "REJECT_ORDER", f"주문ID={order_id}")
        flash(f"주문 {order_id}를 거절 처리했습니다.")
    except:
        db.rollback()
        flash("주문 거절 처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

@app.route("/admin/complete/<int:order_id>", methods=["POST"])
@login_required
def admin_complete(order_id):
    db = get_db_session()
    try:
        o = db.query(Order).filter_by(id=order_id).with_for_update().first()
        if not o or o.status!="paid":
            flash("해당 주문은 'paid' 상태가 아닙니다.")
            return redirect(url_for("admin"))
        o.status = "completed"
        db.commit()
        log_action(session["role"], "COMPLETE_ORDER", f"주문ID={order_id}")
        flash(f"주문 {order_id} 최종 완료되었습니다!")
    except:
        db.rollback()
        flash("주문 완료 처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

# 여러 개 전달하기
@app.route("/admin/deliver_item_count/<int:order_id>/<menu_name>/<int:count>", methods=["POST"])
@login_required
def admin_deliver_item_count(order_id, menu_name, count):
    db = get_db_session()
    try:
        o = db.query(Order).filter_by(id=order_id).with_for_update().first()
        if not o or o.status not in ["paid","completed"]:
            flash("해당 주문 상태가 조리중이 아닙니다.", "error")
            return redirect(url_for("admin"))
        it = next((i for i in o.items if i.menu.name==menu_name), None)
        if not it:
            flash("해당 메뉴가 주문에 없습니다.", "error")
            return redirect(url_for("admin"))
        left_deliver = it.doneQuantity - it.deliveredQuantity
        if left_deliver <= 0:
            flash("더 이상 전달할 수 없습니다.", "error")
            return redirect(url_for("admin"))
        if count > left_deliver:
            flash("요청한 전달 수량이 남은 수량보다 많습니다.", "error")
            return redirect(url_for("admin"))
        it.deliveredQuantity += count
        # 모든 아이템이 deliveredQuantity >= quantity 면 completed
        if all(x.deliveredQuantity >= x.quantity for x in o.items):
            o.status="completed"
        db.commit()
        flash(f"주문 {o.order_id} (ID={o.id}), [{menu_name}] {count}개 전달 완료!")
        log_action(session["role"], "ADMIN_DELIVER_ITEM", f"{order_id}/{menu_name}/{count}")
    except:
        db.rollback()
        flash("전달 처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

# 단일 1개 전달 (호환용)
@app.route("/admin/deliver/<int:order_id>/<menu_name>", methods=["POST"])
@login_required
def admin_deliver_item(order_id, menu_name):
    db = get_db_session()
    try:
        o = db.query(Order).filter_by(id=order_id).with_for_update().first()
        if not o or o.status not in ["paid","completed"]:
            return redirect(url_for("admin"))
        it = next((i for i in o.items if i.menu.name==menu_name), None)
        if not it:
            flash("해당 메뉴가 주문에 없습니다.")
            return redirect(url_for("admin"))
        if it.deliveredQuantity < it.doneQuantity:
            it.deliveredQuantity += 1
        if all(i.deliveredQuantity >= i.quantity for i in o.items):
            o.status="completed"
        db.commit()
        flash(f"주문 {o.order_id} (ID={o.id}), [{menu_name}] 1개 전달!")
        log_action(session["role"], "ADMIN_DELIVER_ITEM", f"{order_id}/{menu_name}")
    except:
        db.rollback()
        flash("전달 처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

@app.route("/admin/soldout/<int:menu_id>", methods=["POST"])
@login_required
def admin_soldout(menu_id):
    db = get_db_session()
    try:
        m = db.query(Menu).filter_by(id=menu_id).first()
        m.sold_out = not m.sold_out
        db.commit()
        log_action(session["role"], "SOLDOUT_TOGGLE", f"메뉴[{m.name}] => soldOut={m.sold_out}")
        flash(f"메뉴 [{m.name}] 품절상태 변경!")
    except:
        db.rollback()
        flash("품절상태 변경 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

@app.route("/admin/log")
@login_required
def admin_log_page():
    db = get_db_session()

    role_filter = request.args.get("role","")
    action_filter = request.args.get("action","")
    detail_filter = request.args.get("detail","")
    time_start    = request.args.get("time_start","")
    time_end      = request.args.get("time_end","")

    q = db.query(Log)
    if role_filter:
        q = q.filter(Log.role.ilike(f"%{role_filter}%"))
    if action_filter:
        q = q.filter(Log.action.ilike(f"%{action_filter}%"))
    if detail_filter:
        q = q.filter(Log.detail.ilike(f"%{detail_filter}%"))
    if time_start.isdigit() and len(time_start)==6:
        q = q.filter(Log.time >= int(time_start))
    if time_end.isdigit() and len(time_end)==6:
        q = q.filter(Log.time <= int(time_end))

    logs = [{
        "time": f"{l.time:06d}",
        "role": l.role,
        "action": l.action,
        "detail": l.detail
    } for l in q.order_by(Log.id.desc())]

    return render_template("admin_log.html", logs=logs,
                           role_filter=role_filter,
                           action_filter=action_filter,
                           detail_filter=detail_filter,
                           time_start=time_start,
                           time_end=time_end)

@app.route("/admin/service", methods=["POST"])
@login_required
def admin_service():
    db = get_db_session()
    table = request.form.get("serviceTable","")
    menu_name = request.form.get("serviceMenu","")
    qty = int(request.form.get("serviceQty","0") or 0)
    if not table or not menu_name or qty<1:
        flash("서비스 등록 실패: 테이블/메뉴/수량 확인 필요", "error")
        return redirect(url_for("admin"))

    if table in _blocked_tables:
        flash("차단된 테이블에는 서비스를 등록할 수 없습니다.", "error")
        return redirect(url_for("admin"))

    m = db.query(Menu).filter_by(name=menu_name).with_for_update().first()
    if m.sold_out:
        flash("해당 메뉴는 품절 상태입니다.", "error")
        return redirect(url_for("admin"))

    now_str = current_hhmmss()
    now_int = int(now_str)
    try:
        new_order = Order(
            order_id=now_str, tableNumber=table,
            peopleCount=0, totalPrice=0, status="paid",
            createdAt=now_int, confirmedAt=now_int, service=True
        )
        db.add(new_order)
        db.flush()
        oi = OrderItem(order_id=new_order.id, menu_id=m.id,
                       quantity=qty, doneQuantity=0, deliveredQuantity=0)
        db.add(oi)
        m.stock -= qty
        if m.stock < 0:
            pass
        if m.category in ["soju","beer","drink"]:
            oi.doneQuantity = qty
        db.commit()
        log_action(session["role"], "ADMIN_SERVICE", f"주문ID={new_order.id}/메뉴:{menu_name}/{qty}")
        flash(f"0원 서비스 주문이 등록되었습니다. (order_id={now_str})")
    except:
        db.rollback()
        flash("서비스 등록 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

@app.route("/admin/update-settings", methods=["POST"])
@login_required
def admin_update_settings():
    update_settings(request.form)
    log_action(session["role"], "UPDATE_SETTINGS", "설정 업데이트")
    flash("설정이 업데이트되었습니다.")
    return redirect(url_for("admin"))

@app.route("/admin/update_stock/<int:menu_id>", methods=["POST"])
@login_required
def admin_update_stock(menu_id):
    db = get_db_session()
    new_stock_str = request.form.get("new_stock","0")
    try:
        new_stock = int(new_stock_str)
    except:
        flash("잘못된 재고 입력값입니다.", "error")
        return redirect(url_for("admin"))

    try:
        m = db.query(Menu).filter_by(id=menu_id).first()
        old_stock = m.stock
        m.stock = new_stock
        db.commit()
        log_action(session["role"], "UPDATE_STOCK", f"메뉴[{m.name}], {old_stock} -> {new_stock}")
        flash(f"메뉴 [{m.name}] 재고가 {new_stock} 으로 수정되었습니다.")
    except:
        db.rollback()
        flash("재고 수정 중 오류가 발생했습니다.", "error")
    return redirect(url_for("admin"))

# ─────────────────────────────────────────────────────────
# 주방 페이지
# ─────────────────────────────────────────────────────────
@app.route("/kitchen")
@login_required
def kitchen():
    db = get_db_session()

    # '만들어야 할 전체 메뉴 수량(미조리 합계)'만 보여줌
    paid_orders = db.query(Order).filter_by(status="paid").all()
    item_count = {}
    for o in paid_orders:
        for it in o.items:
            left = it.quantity - it.doneQuantity
            if left>0:
                item_count[it.menu.name] = item_count.get(it.menu.name, 0) + left

    return render_template("kitchen.html", kitchen_status=item_count)

@app.route("/kitchen/done-item/<menu_name>/<int:count>", methods=["POST"])
@login_required
def kitchen_done_item(menu_name, count):
    db = get_db_session()
    try:
        # paid 상태의 주문들에서 해당 메뉴 미조리 수량 처리
        orders = db.query(Order).filter_by(status="paid").all()
        remaining = count

        for o in orders:
            for it in o.items:
                if it.menu.name==menu_name:
                    left = it.quantity - it.doneQuantity
                    if left>0:
                        if left <= remaining:
                            it.doneQuantity += left
                            remaining -= left
                        else:
                            it.doneQuantity += remaining
                            remaining = 0
                        if remaining<=0:
                            break
            if remaining<=0:
                break

        db.commit()
        log_action(session["role"], "KITCHEN_DONE_ITEM", f"{menu_name} {count}개 조리완료")
        flash(f"[{menu_name}] {count}개 조리 완료 처리.")
    except:
        db.rollback()
        flash("조리 완료 처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("kitchen"))

# ─────────────────────────────────────────────────────────
# 앱 시작
# ─────────────────────────────────────────────────────────
init_db()
start_time_checker()

if __name__=="__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
