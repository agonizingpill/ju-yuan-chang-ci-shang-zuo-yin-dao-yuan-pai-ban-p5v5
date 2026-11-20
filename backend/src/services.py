from datetime import datetime,timedelta,timezone,date
from math import ceil
from fastapi import HTTPException
from sqlalchemy import select
from .models import Production,Performance,CrewCall,Audit,PerformanceCancellation

def get(s,cls,id):
    obj=s.get(cls,id)
    if not obj:raise HTTPException(404,'记录不存在')
    return obj
def pack(v):
    return {c.name:(getattr(v,c.name).isoformat() if isinstance(getattr(v,c.name),date) else getattr(v,c.name)) for c in v.__table__.columns}
def audit(s,u,action):s.add(Audit(actor=u.email,action=action,stamp=datetime.now(timezone.utc).isoformat()))

def lock_production(s,pid):
    return s.scalar(select(Production).where(Production.id==pid).with_for_update().execution_options(populate_existing=True))

def performances(s,pid):
    return s.scalars(select(Performance).where(Performance.production_id==pid).order_by(Performance.date)).all()

def cancellation_map(s,pid):
    rows=s.execute(select(PerformanceCancellation,Performance).join(Performance,Performance.id==PerformanceCancellation.performance_id).where(Performance.production_id==pid)).all()
    return {p.id:(c,p) for c,p in rows}

def calendar_status(s,pid,raise_errors=False):
    """Inspect the training calendar without mutating anything.

    A registered cancellation is a deliberate non-performance day: it closes the
    gap without becoming a zero-attendance observation. An existing zero value on
    a performance without a cancellation record stays a real attendance day, so
    legacy data and genuine empty houses are unaffected. A date with no record at
    all, or no ticket count and no cancellation, is still a missing day that blocks
    forecasting.
    """
    rows=performances(s,pid);cancelled=cancellation_map(s,pid)
    recorded=[r for r in rows if r.arrivals is not None and r.id not in cancelled]
    missing=[]
    if recorded:
        by_date={r.date:r for r in rows};day=recorded[0].date
        while day<=recorded[-1].date:
            row=by_date.get(day)
            if row is None or (row.arrivals is None and row.id not in cancelled):missing.append(day)
            day+=timedelta(days=1)
    error=None
    if len(recorded)<28:error='至少需要 28 天完整验票记录（停演日不计入训练）'
    elif missing:error=f'验票日历缺口：{missing[0].isoformat()}；请先补齐验票或由场务登记停演'
    if error and raise_errors:raise HTTPException(409,error)
    return {'production_id':pid,'rows':rows,'recorded':recorded,'cancelled':cancelled,'missing':missing,
            'first':recorded[0].date if recorded else None,'last':recorded[-1].date if recorded else None,'error':error}

def _reset_pending(s,production):
    perfs=s.scalars(select(Performance).where(Performance.production_id==production.id,Performance.arrivals.is_(None))).all()
    for p in perfs:
        if p.status in ('crew_confirmed','cancelled'):continue
        p.forecast=None;p.raw_forecast=None;p.status='scheduled'
        call=s.scalar(select(CrewCall).where(CrewCall.performance_id==p.id))
        if call and call.status!='confirmed':call.status='invalidated'

def invalidate(s,production):
    if production.forecast_state=='calculating':raise HTTPException(409,'预测进行中，请稍后修改验票输入')
    perfs=s.scalars(select(Performance).where(Performance.production_id==production.id,Performance.arrivals.is_(None))).all()
    if any(p.status=='crew_confirmed' for p in perfs):raise HTTPException(409,'已有确认点名，历史与剧目输入已冻结')
    _reset_pending(s,production)
    production.forecast_state='idle';production.forecast_evidence=None

def _drop_published_date(production,perf):
    """Remove one date from an already-published week without republishing the rest."""
    evidence=production.forecast_evidence
    if not isinstance(evidence,dict):return
    date=perf.date.isoformat()
    future=[f for f in evidence.get('future') or [] if f.get('date')!=date]
    skipped=[x for x in evidence.get('skipped_future') or [] if x.get('date')!=date]
    skipped.append({'date':date,'reason':'停演'})
    production.forecast_evidence={**evidence,'future':future,'skipped_future':skipped}

def cancel_performance(s,pid,day,reason,u):
    # Production is the publication lock shared with the forecast worker: a
    # cancellation committed first is seen by that run, and one that waits for it
    # clears the freshly published date instead of old numbers restoring it.
    production=lock_production(s,pid)
    perf=s.scalar(select(Performance).where(Performance.production_id==pid,Performance.date==day).with_for_update().execution_options(populate_existing=True))
    cancelled=cancellation_map(s,pid)
    if perf is None:
        recorded=[r for r in performances(s,pid) if r.arrivals is not None and r.id not in cancelled]
        if not recorded or not recorded[0].date<=day<=recorded[-1].date+timedelta(days=7):
            raise HTTPException(409,'该日期不在可登记停演的日历范围内；请先登记场次或补齐验票')
        # A day with no performance record can still be a deliberate dark night;
        # it becomes a cancellation slot rather than a fabricated zero audience.
        perf=Performance(production_id=pid,date=day,curtain='19:30',status='scheduled');s.add(perf);s.flush()
    if perf.arrivals is not None:raise HTTPException(409,'本场已经录有验票，不能登记停演；零人场仍按实际零人计入训练')
    if perf.id in cancelled or perf.status=='cancelled':raise HTTPException(409,'本场已登记停演')
    call=s.scalar(select(CrewCall).where(CrewCall.performance_id==perf.id).with_for_update())
    if call and call.status=='confirmed':raise HTTPException(409,'本场点名已确认，不能登记停演；请先由经理处理该点名')
    recorded=[r for r in performances(s,pid) if r.arrivals is not None and r.id not in cancelled]
    affects_history=bool(recorded) and perf.date<=recorded[-1].date
    if affects_history and any(p.status=='crew_confirmed' for p in performances(s,pid)):
        raise HTTPException(409,'已有确认点名，历史验票输入已冻结，不能补登停演')
    perf.forecast=None;perf.raw_forecast=None;perf.status='cancelled'
    if call:call.status='invalidated'
    s.add(PerformanceCancellation(performance_id=perf.id,reason=reason.strip(),registered_by=u.email,registered_at=datetime.now(timezone.utc).isoformat()))
    if affects_history:
        # The training calendar changed, so every unpublished number is now stale.
        _reset_pending(s,production);production.forecast_state='idle';production.forecast_evidence=None
    else:
        # A cancelled date inside the coming week leaves the other days' numbers valid.
        _drop_published_date(production,perf)
    audit(s,u,f'登记停演 场次#{perf.id} {perf.date.isoformat()}')
    return perf

def save_crew(s,data):
    preview=get(s,Performance,data['performance_id'])
    production=lock_production(s,preview.production_id)
    perf=s.scalar(select(Performance).where(Performance.id==preview.id).with_for_update().execution_options(populate_existing=True))
    if perf.status=='cancelled':raise HTTPException(409,'本场已登记停演，不能编制点名')
    if production.forecast_state=='calculating':raise HTTPException(409,'预测更新中，暂不能编制点名')
    if perf.status!='forecasted' or perf.forecast is None:raise HTTPException(409,'请先为本场完成上座预测；已确认场次不能改班')
    curtain=datetime.fromisoformat(perf.date.isoformat()+'T'+perf.curtain)
    call=data['call_time'].replace(tzinfo=None)
    if call>curtain-timedelta(minutes=30) or call<curtain-timedelta(hours=6):raise HTTPException(409,'点名须在开场前 30 分钟至 6 小时内')
    v=s.scalar(select(CrewCall).where(CrewCall.performance_id==perf.id).with_for_update())
    if v and v.status=='confirmed':raise HTTPException(409,'已确认点名不可修改')
    if not v:v=CrewCall(performance_id=perf.id,ushers=data['ushers'],call_time=call.isoformat(),leader=data['leader']);s.add(v)
    else:v.ushers=data['ushers'];v.call_time=call.isoformat();v.leader=data['leader'];v.status='proposed'
    return v

def confirm(s,cid):
    # Same lock order as cancellation: production, performance, then call.
    preview=s.get(CrewCall,cid)
    if not preview:raise HTTPException(404,'点名单不存在')
    preview_perf=s.get(Performance,preview.performance_id)
    prod=lock_production(s,preview_perf.production_id)
    perf=s.scalar(select(Performance).where(Performance.id==preview.performance_id).with_for_update().execution_options(populate_existing=True))
    call=s.scalar(select(CrewCall).where(CrewCall.id==cid).with_for_update().execution_options(populate_existing=True))
    if perf.status=='cancelled':raise HTTPException(409,'本场已登记停演，不能确认点名')
    if call.status!='proposed' or perf.status!='forecasted' or prod.forecast_state!='ready':raise HTTPException(409,'点名或预测证据已失效，请重新编排')
    need=max(1,ceil(perf.forecast/40))
    if call.ushers<need:raise HTTPException(409,f'引导员不足：预计 {perf.forecast} 人，至少需 {need} 名')
    if perf.forecast>prod.seats:raise HTTPException(409,'本场预测超过座位数，请重新核对容量')
    call.status='confirmed';perf.status='crew_confirmed'
    return call
