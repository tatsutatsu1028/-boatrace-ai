-- note 自動下書き: 万舟チャンスのレースの記事の価格（厳選10とは別。初期値は今と同じ300円）と、対象を「厳選＋万舟」に戻す。
--
--   app_settings.note_auto_price_manshu : 万舟チャンスのレースの記事の価格（円）。設定タブで変える（note_auto_view.py）
--   万舟チャンスのレースの記事には、万舟チャンスの買い目（展示後に作り直したもの）と通常の予想（本線・抑え）の2種類を載せる
--   （note_auto.py。タイトルの頭に【万舟チャンス】）。厳選10の記事は300円・今までの形のまま。

alter table public.app_settings
  add column if not exists note_auto_price_manshu integer not null default 300;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'app_settings_note_auto_price_manshu_check') then
    alter table public.app_settings add constraint app_settings_note_auto_price_manshu_check
      check (note_auto_price_manshu between 100 and 50000);
  end if;
end $$;

comment on column public.app_settings.note_auto_price_manshu is
  'note 自動下書き: 万舟チャンスのレースの記事の価格（円）。厳選10の記事は300円';

update public.app_settings set note_auto_scope = 'pre_manshu' where id = 1 and note_auto_scope = 'pre';
