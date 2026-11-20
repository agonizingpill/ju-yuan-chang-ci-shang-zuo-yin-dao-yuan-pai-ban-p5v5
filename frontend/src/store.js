import {api} from './api.js';
export const store={user:null,productions:[],performances:[],calls:[],staff:[],assignments:[],debriefs:[],zones:[],posts:[],incidents:[],items:[],itemClaims:[],cancellations:[],calendarStatus:[],productionId:null};
export async function refresh(){[store.productions,store.performances,store.calls,store.staff,store.assignments,store.debriefs,store.zones,store.posts,store.incidents,store.items,store.itemClaims,store.cancellations,store.calendarStatus]=await Promise.all([api('/productions'),api('/performances'),api('/crew-calls'),api('/staff'),api('/crew-assignments'),api('/show-debriefs'),api('/entrance-zones'),api('/zone-posts'),api('/house-incidents'),api('/lost-items'),api('/item-claims'),api('/cancellations'),api('/calendar-status')]);if(!store.productionId&&store.productions.length)store.productionId=store.productions[0].id;return store}
export const production=s=>s.productions.find(x=>x.id===Number(s.productionId));
export const performances=s=>s.performances.filter(x=>x.production_id===Number(s.productionId));
export const cancelledIds=s=>new Set(s.cancellations.filter(x=>x.production_id===Number(s.productionId)).map(x=>x.performance_id));
export const calendar=s=>s.calendarStatus.find(x=>x.production_id===Number(s.productionId));
export const upcoming=s=>{const rows=performances(s).filter(x=>x.arrivals===null),last=performances(s).filter(x=>x.arrivals!==null).at(-1);return rows.filter(x=>!last||x.date>last.date).sort((a,b)=>a.date.localeCompare(b.date)).slice(0,7)};
