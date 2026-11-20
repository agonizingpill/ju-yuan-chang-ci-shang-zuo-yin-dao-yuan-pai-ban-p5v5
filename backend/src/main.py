import os,logging
from contextlib import asynccontextmanager
import redis
from fastapi import FastAPI,Depends,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import Base,engine,db_session
from .models import User,Production,Performance,CrewCall,Audit,PerformanceCancellation
from .auth import current,writers,admin,verify,token
from .schemas import Login,ProductionInput,PerformanceInput,ArrivalInput,CrewInput,CancellationInput,DayCancellationInput
from .services import get,pack,audit,calendar_status,invalidate,save_crew,confirm,cancel_performance,lock_production
from .middlewares import RequestTrace,BurstGuard
from .frontdesk_routes import router as frontdesk_router
from .house_routes import router as house_router
from .constants import ROLES,PERFORMANCE_STATES,CALL_STATES
r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    if os.getenv('SEED_DEMO','true')=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:logging.exception('seed failed')
    yield
app=FastAPI(title='南岸 · 场次上座与场务点名',lifespan=lifespan)
app.include_router(frontdesk_router)
app.include_router(house_router)
app.add_middleware(RequestTrace);app.add_middleware(BurstGuard)
@app.exception_handler(IntegrityError)
async def conflict(req,exc):return JSONResponse({'detail':'剧目编号、场次日期或点名单重复，请选择已有记录修订'},409)
@app.get('/health')
def health(s=Depends(db_session)):s.execute(select(1));r.ping();return {'status':'ok'}
@app.post('/auth/login')
def login(p:Login,s=Depends(db_session)):
    u=s.scalar(select(User).where(User.email==p.email))
    if not u or not verify(p.password,u.password_hash):raise HTTPException(401,'邮箱或口令不正确')
    return {'token':token(u),'user':{'email':u.email,'role':u.role}}
@app.get('/me')
def me(u=Depends(current)):return {'email':u.email,'role':u.role}
@app.get('/enums')
def enums(u=Depends(current)):return {'roles':ROLES,'performance_states':PERFORMANCE_STATES,'call_states':CALL_STATES}
@app.get('/productions')
def productions(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(Production).order_by(Production.id))]
@app.post('/productions')
def add_production(p:ProductionInput,s=Depends(db_session),u=Depends(admin)):
    v=Production(**p.model_dump());s.add(v);audit(s,u,'登记剧目 '+p.code);s.commit();return pack(v)
@app.put('/productions/{pid}')
def edit_production(pid:int,p:ProductionInput,s=Depends(db_session),u=Depends(admin)):
    # Lock the publication slot first so a concurrent cancellation cannot clear an
    # old week and then be overwritten by this revision's stale forecast evidence.
    lock_production(s,pid)
    v=get(s,Production,pid);invalidate(s,v)
    if s.scalar(select(Performance.id).where(Performance.production_id==pid,Performance.arrivals>p.seats)):raise HTTPException(409,'座位数小于既有验票人数，不能修改')
    for k,value in p.model_dump().items():setattr(v,k,value)
    audit(s,u,'修订剧目 '+v.code);s.commit();return pack(v)
@app.get('/performances')
def performances(production_id:int|None=None,s=Depends(db_session),u=Depends(current)):
    q=select(Performance).order_by(Performance.date)
    if production_id:q=q.where(Performance.production_id==production_id)
    return [pack(v) for v in s.scalars(q)]
@app.post('/performances')
def add_performance(p:PerformanceInput,s=Depends(db_session),u=Depends(writers)):
    get(s,Production,p.production_id);prod=lock_production(s,p.production_id)
    if p.arrivals is not None and p.arrivals>prod.seats:raise HTTPException(409,'验票人数超过本场座位容量')
    if p.arrivals is not None and s.scalar(select(PerformanceCancellation.id).join(Performance,Performance.id==PerformanceCancellation.performance_id).where(Performance.production_id==prod.id,Performance.date==p.date)):raise HTTPException(409,'该日期已登记停演，不能再补录验票；如属误登，请由经理处理停演记录')
    invalidate(s,prod)
    v=Performance(**p.model_dump(),status='observed' if p.arrivals is not None else 'scheduled');s.add(v);audit(s,u,'登记场次 '+p.date.isoformat());s.commit();return pack(v)
@app.patch('/performances/{fid}/arrivals')
def arrivals(fid:int,p:ArrivalInput,s=Depends(db_session),u=Depends(writers)):
    perf=get(s,Performance,fid);prod=lock_production(s,perf.production_id)
    perf=s.scalar(select(Performance).where(Performance.id==fid).with_for_update().execution_options(populate_existing=True))
    if p.arrivals>prod.seats:raise HTTPException(409,'验票人数超过本场座位容量')
    if perf.status=='cancelled':raise HTTPException(409,'本场已登记停演，不能补录验票')
    if perf.arrivals is None:raise HTTPException(409,'未来预测场次不能直接写验票；请通过新场次补录历史')
    invalidate(s,prod);perf.arrivals=p.arrivals;audit(s,u,'修订验票 '+str(perf.date));s.commit();return pack(perf)
@app.post('/productions/{pid}/forecast')
def forecast(pid:int,s=Depends(db_session),u=Depends(writers)):
    prod=s.query(Production).filter_by(id=pid).with_for_update().one_or_none()
    if not prod:raise HTTPException(404,'剧目不存在')
    if s.scalar(select(Performance.id).where(Performance.production_id==pid,Performance.status=='crew_confirmed')):raise HTTPException(409,'已有确认点名，预测证据已冻结')
    if prod.forecast_state=='calculating':raise HTTPException(409,'预测进行中')
    # Publish the calculating state before enqueueing so the worker always sees it.
    # Anything that invalidates this run afterwards (cancellation, input revision)
    # bumps the state away from calculating, and the worker drops its stale result.
    calendar_status(s,pid,raise_errors=True);invalidate(s,prod);prod.forecast_state='calculating';audit(s,u,'预测入队 '+prod.code);s.commit()
    try:r.rpush('theatre-forecast',pid)
    except Exception:prod.forecast_state='failed';s.commit();raise HTTPException(503,'预测队列暂不可用，请重试')
    return pack(prod)
@app.post('/performances/{fid}/cancellation')
def cancel(fid:int,p:CancellationInput,s=Depends(db_session),u=Depends(writers)):
    perf=get(s,Performance,fid);v=cancel_performance(s,perf.production_id,perf.date,p.reason,u);s.commit();return pack(v)
@app.post('/productions/{pid}/cancellations')
def cancel_day(pid:int,p:DayCancellationInput,s=Depends(db_session),u=Depends(writers)):
    get(s,Production,pid);v=cancel_performance(s,pid,p.date,p.reason,u);s.commit();return pack(v)
@app.get('/cancellations')
def cancellations(production_id:int|None=None,s=Depends(db_session),u=Depends(current)):
    q=select(PerformanceCancellation,Performance).join(Performance,Performance.id==PerformanceCancellation.performance_id).order_by(Performance.date)
    if production_id:q=q.where(Performance.production_id==production_id)
    return [dict(pack(c),date=p.date.isoformat(),curtain=p.curtain,production_id=p.production_id) for c,p in s.execute(q).all()]
def _status_view(ct):
    return {'production_id':ct['production_id'],'first':ct['first'].isoformat() if ct['first'] else None,'last':ct['last'].isoformat() if ct['last'] else None,'recorded_days':len(ct['recorded']),'cancelled_days':sorted(p.date.isoformat() for _,p in ct['cancelled'].values()),'missing_days':[d.isoformat() for d in ct['missing']],'ready':ct['error'] is None,'error':ct['error']}
@app.get('/calendar-status')
def status_all(s=Depends(db_session),u=Depends(current)):
    return [_status_view(calendar_status(s,pid)) for pid in s.scalars(select(Production.id).order_by(Production.id))]
@app.get('/productions/{pid}/calendar-status')
def status(pid:int,s=Depends(db_session),u=Depends(current)):
    get(s,Production,pid);return _status_view(calendar_status(s,pid))
@app.get('/crew-calls')
def crews(s=Depends(db_session),u=Depends(current)):return [pack(v) for v in s.scalars(select(CrewCall).order_by(CrewCall.id))]
@app.post('/crew-calls')
def crew(p:CrewInput,s=Depends(db_session),u=Depends(writers)):
    v=save_crew(s,p.model_dump());audit(s,u,'编排点名 场次#'+str(p.performance_id));s.commit();return pack(v)
@app.post('/crew-calls/{cid}/confirm')
def confirm_call(cid:int,s=Depends(db_session),u=Depends(admin)):
    v=confirm(s,cid);audit(s,u,'确认点名#'+str(cid));s.commit();return pack(v)
@app.get('/audits')
def audits(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(Audit).order_by(Audit.id.desc()).limit(30))]
