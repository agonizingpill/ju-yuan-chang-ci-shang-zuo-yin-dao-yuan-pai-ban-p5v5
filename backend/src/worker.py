import os,json,logging
from pathlib import Path
import redis
from .db import Session
from .models import Production
from .forecast import forecast_week,StaleForecast
r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
Path('/artifacts').mkdir(exist_ok=True)
while True:
    Path('/tmp/worker-heartbeat').touch()
    entry=r.blpop('theatre-forecast',timeout=2)
    if not entry:continue
    with Session() as s:
        p=s.get(Production,int(entry[1]))
        if not p or p.forecast_state!='calculating':continue
        try:
            p.forecast_evidence=forecast_week(s,p);p.forecast_state='ready'
            Path(f'/artifacts/production-{p.id}.json').write_text(json.dumps(p.forecast_evidence,ensure_ascii=False))
            s.commit()
        except StaleForecast as e:
            # A cancellation (or an input revision) landed while this run was fitting.
            # The publication slot is already idle; drop the result rather than restore
            # a week that was just superseded.
            s.rollback();logging.info('forecast discarded for production %s: %s',entry[1],e)
        except Exception as e:
            s.rollback();p=s.get(Production,int(entry[1]))
            if p and p.forecast_state=='calculating':p.forecast_state='failed';p.forecast_evidence={'error':str(e)};s.commit()
            logging.exception('forecast failed')
