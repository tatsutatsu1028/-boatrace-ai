-- 全レース資金配分への切り替え（「1着確率差40ポイント未満は非推奨・賭け金0円」の廃止）に伴う列追加。
--
--   prediction_results.hit_probability : 固定時に画面へ表示した「この買い目の的中確率」
--                                        （各買い目の3連単確率の合計, 0〜1）
--   prediction_results.stake_policy    : 資金配分の方針。切り替え後は 'all_races'、切り替え前の行は NULL
--   app_settings.stake_policy_switched_at : 切り替えた日時（検証画面で切り替え前後を分けて表示する目安）
--
-- いずれも NULL 許容の列追加だけなので、切り替え前のコードからも読み書きできる。
-- stake_policy_switched_at は、アプリと自動固定に新方式が反映された時点で
--   update public.app_settings set stake_policy_switched_at = now() where id = 1 and stake_policy_switched_at is null;
-- を実行して記録する。

alter table public.prediction_results
  add column if not exists hit_probability double precision,
  add column if not exists stake_policy text;

alter table public.app_settings
  add column if not exists stake_policy_switched_at timestamp with time zone;
