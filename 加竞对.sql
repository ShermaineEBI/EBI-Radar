-- ============================================================
-- 加竞对专用。只跑这一段,永远不要重跑 schema.sql 的整段。
-- 账号名:纯账号名,不要 @,不要链接。重复跑不会报错。
-- ============================================================
insert into competitors (username, tracker, active) values
  ('新竞对1', 'IG', true),
  ('新竞对2', 'IG', true),
  ('新竞对3', 'IG', true)
on conflict do nothing;

-- ---------- Facebook 专页 ----------
-- 填专页网址最后那一段(facebook.com/ 后面的名字),tracker 写 'FB'。
--   ✅ QashierMY      ✅ https://www.facebook.com/QashierMY      ❌ 专页显示名称
-- 没有名字只有数字的专页:填 profile.php?id= 后面那串数字。
insert into competitors (username, tracker, active) values
  ('新FB专页1', 'FB', true)
on conflict do nothing;

-- 想停掉某个竞对(保留数据,只是不再抓):
-- update competitors set active = false where lower(username) = lower('要停的账号名');

-- 看现在在追踪哪些:
-- select username, tracker, active, notes from competitors order by active desc, username;
