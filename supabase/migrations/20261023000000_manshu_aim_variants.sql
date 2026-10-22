-- 「💥 万舟チャンス」の中穴〜万舟狙いの買い目の作り方の比較（検証だけ。本番・画面の買い目は変えない）。
-- manshu_compare.py が、朝版・展示後版それぞれで次の作り方の買い目を確定オッズで作り、結果を残す
-- （どれも1号艇1着を外す・オッズ30倍以上・計10点・各100円）。
--   A: 確率順・中穴5点＋万舟5点（今の作り方） / B: 期待値順（確率×オッズ）・中穴5点＋万舟5点
--   C: 確率順・中穴3点＋万舟7点            / D: 期待値順・中穴3点＋万舟7点
-- 「1号艇の1着確率が50%以上のレースは見送る」版は、p1 で集計時に絞る（manshu.variant_summary）。
--
--   variants        : {"pre"|"post": {"p1": 1号艇の1着確率, "error": 作れなかった理由,
--                                     "A".."D": {"hit", "stake", "return", "prob_sum"（買い目の確率の合計）}}}
--   variant_tickets : {"pre"|"post": {"A".."D": [{combo, prob, odds, tier, stake, payout}]}}
--
-- 10/17 以降の既存の行は、次の manshu_compare.py（hindcast.yml の事後予想の後）が保存済みの確定オッズで埋める。

alter table public.manshu_aim_compare
  add column if not exists variants jsonb,
  add column if not exists variant_tickets jsonb;
