
from __future__ import annotations
import itertools
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from comment_analyzer import total_comment_score

BASE_NUM = ["race_no","lane","racer_win_rate","local_win_rate","motor_2ren","boat_2ren","avg_st"]
BASE_CAT = ["venue"]

def _pipeline():
    prep=ColumnTransformer([
        ("num",Pipeline([("impute",SimpleImputer(strategy="median")),("scale",StandardScaler())]),BASE_NUM),
        ("cat",Pipeline([("impute",SimpleImputer(strategy="most_frequent")),("ohe",OneHotEncoder(handle_unknown="ignore", sparse_output=False))]),BASE_CAT)
    ])
    clf=HistGradientBoostingClassifier(max_iter=220,learning_rate=.06,max_leaf_nodes=31,l2_regularization=1.0,random_state=42)
    return Pipeline([("prep",prep),("clf",clf)])

def train(history):
    need=set(BASE_NUM+BASE_CAT+["finish"])
    missing=need-set(history.columns)
    if missing: raise ValueError("学習CSVに不足列: "+", ".join(sorted(missing)))
    m=_pipeline()
    m.fit(history[BASE_NUM+BASE_CAT],(pd.to_numeric(history["finish"],errors="coerce")==1).astype(int))
    return m

def _rank_score_lower_better(series):
    s=pd.to_numeric(series,errors="coerce")
    if s.notna().sum()<2: return np.zeros(len(s))
    r=s.rank(method="average",ascending=True)
    mid=(s.notna().sum()+1)/2
    z=(mid-r)/(max(1,s.notna().sum()-1)/2)
    return z.fillna(0).to_numpy()

def _rank_score_higher_better(series):
    return -_rank_score_lower_better(series)

def predict(model, race, display_weight=.32, comment_weight=.18):
    x=race.copy()
    for c in BASE_NUM+BASE_CAT:
        if c not in x: x[c]=np.nan
    raw=model.predict_proba(x[BASE_NUM+BASE_CAT])[:,1]
    raw=np.clip(raw,1e-6,None)

    adjustment=np.zeros(len(x))
    reasons=[[] for _ in range(len(x))]

    if "exhibition_time" in x:
        z=_rank_score_lower_better(x["exhibition_time"])
        adjustment += display_weight*z
        for i,v in enumerate(z):
            if v>.55: reasons[i].append("展示タイム上位")
            elif v<-.55: reasons[i].append("展示タイム下位")

    for col,label,w,lower in [
        ("original_straight","直線展示",.11,True),
        ("original_turn","まわり足展示",.11,True),
        ("original_lap","1周展示",.08,True),
    ]:
        if col in x:
            z=_rank_score_lower_better(x[col]) if lower else _rank_score_higher_better(x[col])
            adjustment += w*z
            for i,v in enumerate(z):
                if v>.65: reasons[i].append(label+"上位")

    if "exhibition_st" in x:
        st=pd.to_numeric(x["exhibition_st"],errors="coerce")
        # reward 0.00~0.12; penalize F (negative) and very late.
        st_adj=np.where(st<0,-.35,np.where(st<=.12,.18,np.where(st>.22,-.16,0)))
        st_adj=np.nan_to_num(st_adj)
        adjustment += .16*st_adj
        for i,v in enumerate(st):
            if pd.notna(v) and v<0: reasons[i].append("展示F")
            elif pd.notna(v) and 0<=v<=.08: reasons[i].append("展示ST早め")

    if "comment" in x:
        cs=np.array([total_comment_score(v) for v in x["comment"]])
        adjustment += comment_weight*cs
        for i,v in enumerate(cs):
            if v>.30: reasons[i].append("コメント好感")
            elif v<-.30: reasons[i].append("コメント弱め")

    # weight/tilt remain visible but v2 avoids hard-coded assumptions without historical calibration.
    strength=raw*np.exp(adjustment)
    p=strength/strength.sum()
    out=x[["lane"]].copy()
    if "racer_name" in x: out["racer_name"]=x["racer_name"]
    out["p_first"]=p
    out["adjustment"]=adjustment
    out["reason"]=[" / ".join(r) if r else "基礎データ中心" for r in reasons]
    return out.sort_values("lane")

def trifecta(first):
    s=dict(zip(first["lane"].astype(int),first["p_first"].astype(float)))
    rows=[]
    for a,b,c in itertools.permutations(range(1,7),3):
        pa=s[a]/sum(s.values())
        pb=s[b]/sum(v for k,v in s.items() if k!=a)
        pc=s[c]/sum(v for k,v in s.items() if k not in (a,b))
        rows.append((f"{a}-{b}-{c}",pa*pb*pc))
    return pd.DataFrame(rows,columns=["combo","prob"])

def rank_tickets(tri, odds=None, main_n=3, cover_n=3, longshot_n=2):
    x=tri.copy()
    if odds is not None and len(odds):
        x=x.merge(odds[["combo","odds"]],on="combo",how="left")
    else:
        x["odds"]=np.nan
    x["expected_return"]=x["prob"]*x["odds"]

    # 本線: probability-led, but EV breaks ties.
    main=x.sort_values(["prob","expected_return"],ascending=False).head(main_n).copy()
    used=set(main["combo"])
    rem=x[~x["combo"].isin(used)].copy()

    # 抑え: medium-high probability + reasonable EV.
    rem["cover_score"]=rem["prob"].rank(pct=True)*.72 + rem["expected_return"].fillna(0).clip(upper=2).rank(pct=True)*.28
    cover=rem.sort_values("cover_score",ascending=False).head(cover_n).copy()
    used |= set(cover["combo"])
    rem=x[~x["combo"].isin(used)].copy()

    # 穴: not top probability, but seek price/EV; without odds use lower-prob plausible combos.
    if rem["odds"].notna().sum():
        rem["long_score"]=rem["expected_return"].fillna(0)*.72 + np.log1p(rem["odds"].fillna(0))*.08 + rem["prob"]*5
    else:
        rem["long_score"]=rem["prob"]
        rem=rem.iloc[max(0,len(rem)//5):]
    longshot=rem.sort_values("long_score",ascending=False).head(longshot_n).copy()
    main["group"]="本線"; cover["group"]="抑え"; longshot["group"]="穴"
    return pd.concat([main,cover,longshot],ignore_index=True)

def confidence(first, race):
    p=np.sort(first["p_first"].to_numpy())[::-1]
    margin=p[0]-p[1]
    completeness=np.mean([pd.notna(race.get(c,pd.Series([np.nan]*len(race)))).mean()
                          for c in ["racer_win_rate","motor_2ren","exhibition_time"]])
    score=margin*.9+completeness*.18
    return "A" if score>.33 else "B" if score>.20 else "C"
