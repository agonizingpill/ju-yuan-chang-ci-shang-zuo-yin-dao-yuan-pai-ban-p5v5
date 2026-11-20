from datetime import timedelta
from math import ceil
import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from sqlalchemy import select
from .services import calendar_status
from .models import Production,Performance

class StaleForecast(Exception):
    """The publication slot was invalidated while this run was waiting to publish."""

def fit(values):return ExponentialSmoothing(np.array(values,dtype=float),trend='add',seasonal='add',seasonal_periods=7,initialization_method='estimated').fit(optimized=True)

def fill_gap(values):
    """Fill a non-performance day without teaching the model that it was empty.

    The daily index keeps every calendar date so the weekly season stays aligned.
    A cancelled day borrows its level from observations on the same weekday
    (linear in between), falling back to the nearest neighbours only when no such
    observation exists. Filling with 0 would drag the surrounding week down.
    """
    out=[None if v is None else float(v) for v in values];known=[i for i,v in enumerate(out) if v is not None]
    if not known:return [0.0]*len(out)
    for i,v in enumerate(out):
        if v is not None:continue
        same=[j for j in known if (j-i)%7==0]
        pool=same or known
        before=[j for j in pool if j<i];after=[j for j in pool if j>i]
        if before and after:
            a,b=before[-1],after[0];out[i]=out[a]+(out[b]-out[a])*(i-a)/(b-a)
        elif before:out[i]=out[before[-1]]
        else:out[i]=out[after[0]]
    return out

def forecast_week(s,production):
    # Production is the publication lock shared with cancellation registration.
    # A cancellation committed first is visible here; one that waits behind this
    # lock clears this run's output instead of being overwritten by it.
    locked=s.scalar(select(Production).where(Production.id==production.id).with_for_update().execution_options(populate_existing=True))
    if locked.forecast_state!='calculating':raise StaleForecast('预测发布前已被停演或输入修订作废')
    status=calendar_status(s,production.id,raise_errors=True)
    recorded=status['recorded'];cancelled=status['cancelled']
    by_date={r.date:r for r in status['rows']}
    days=[];day=recorded[0].date
    while day<=recorded[-1].date:
        row=by_date.get(day)
        closed=row is not None and row.id in cancelled
        days.append({'date':day,'value':None if row is None or closed or row.arrivals is None else float(row.arrivals),'cancelled':closed})
        day+=timedelta(days=1)
    holdout=7
    if len(days)<=holdout:raise ValueError('历史天数不足，无法留出最近七天回测')
    train_days,holdout_days=days[:-holdout],days[-holdout:]
    # Holdout week is never fed back into the training fit.
    back=fit(fill_gap([d['value'] for d in train_days])).forecast(holdout)
    backtest=[];errors=[]
    for item,value in zip(holdout_days,back):
        if item['value'] is None:
            backtest.append({'date':item['date'].isoformat(),'actual':None,'predicted':round(float(value),2),'counted':False,'reason':'停演' if item['cancelled'] else '无验票'})
            continue
        error=abs(item['value']-float(value));errors.append(error)
        backtest.append({'date':item['date'].isoformat(),'actual':int(item['value']),'predicted':round(float(value),2),'counted':True,'error':round(error,2)})
    if not errors:raise ValueError('最近七天没有可参与误差计算的开演日，请核对停演与验票记录')
    mae=float(np.mean(errors))
    pred=fit(fill_gap([d['value'] for d in days])).forecast(holdout)
    output=[];skipped=[]
    for i,value in enumerate(pred):
        day=recorded[-1].date+timedelta(days=i+1)
        perf=s.scalar(select(Performance).where(Performance.production_id==production.id,Performance.date==day).with_for_update().execution_options(populate_existing=True))
        if perf is not None and perf.id in cancelled:
            # Original date keeps its slot; a cancelled day yields no forecast and no call.
            skipped.append({'date':day.isoformat(),'reason':'停演'})
            perf.forecast=None;perf.raw_forecast=None;perf.status='cancelled'
            continue
        if perf and (perf.arrivals is not None or perf.status=='crew_confirmed'):raise ValueError('未来场次已有验票或确认点名，不得覆盖')
        if not perf:perf=Performance(production_id=production.id,date=day,curtain='19:30',status='scheduled');s.add(perf)
        raw=max(0,float(value));perf.raw_forecast=round(raw,2);perf.forecast=min(production.seats,ceil(raw));perf.status='forecasted'
        output.append({'date':day.isoformat(),'raw':round(raw,2),'capped':perf.forecast,'required_ushers':max(1,ceil(perf.forecast/40))})
    return {'backtest_mae':round(mae,3),'train_days':len(train_days),'train_recorded_days':sum(d['value'] is not None for d in train_days),'holdout_days':holdout,'history_days':len(recorded),'holdout_start':holdout_days[0]['date'].isoformat(),'history_end':recorded[-1].date.isoformat(),'backtest':backtest,'error_days':[r['date'] for r in backtest if r['counted']],'cancelled_days':[p.date.isoformat() for _,p in cancelled.values() if recorded[0].date<=p.date<=recorded[-1].date],'missing_days':[d.isoformat() for d in status['missing']],'future':output,'skipped_future':skipped,'method':'Holt-Winters additive trend + additive weekly seasonality (7); 停演日不补零、不进误差','seat_capacity':production.seats}
