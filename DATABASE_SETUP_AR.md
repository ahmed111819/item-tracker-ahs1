# إعداد Amazon.sa deal scanners على PostgreSQL في Oracle

هذا المستودع يشغّل البوتات الثلاثة بالتوازي في GitHub Actions. سيرفر Oracle يستخدم للتخزين فقط؛ لا يشغّل فحص Amazon. اتصال GitHub Actions بقاعدة البيانات يمر عبر نفق SSH مقيّد إلى PostgreSQL المحلي، لذلك لا نفتح المنفذ 5432 على الإنترنت.

## حالة الترحيل الحالية

تم استيراد الحالات الموجودة للبوتات الثلاثة وسجل التنبيهات المشترك إلى قاعدة `coupon_storage` على السيرفر:

- ثلاث حالات بوتات: `bot1` و`bot2` و`bot3`.
- 43 سجل تنبيه قديم.
- علامة اكتمال الاستيراد موجودة، والماسحات ترفض البدء إذا غابت الحالة.

ملفات JSON الأصلية لم تُرفع إلى GitHub. أزيلت نسخ الرفع المؤقتة من السيرفر بعد الاستيراد.

## صلاحيات PostgreSQL

قاعدة البيانات `coupon_storage` والجداول موجودة على السيرفر. أنشئ مستخدم تشغيل محدودًا، ثم امنحه الصلاحيات التالية. عيّن كلمة مرور للمستخدم باستخدام `\\password coupon_bot` داخل `psql`؛ لا ترسل كلمة المرور في المحادثة ولا تحفظها في المستودع.

```sql
CREATE ROLE coupon_bot LOGIN;
GRANT CONNECT ON DATABASE coupon_storage TO coupon_bot;
GRANT USAGE ON SCHEMA public TO coupon_bot;
GRANT SELECT, INSERT, UPDATE ON public.bot_state TO coupon_bot;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.alert_registry TO coupon_bot;
GRANT SELECT ON public.storage_migrations TO coupon_bot;
GRANT EXECUTE ON FUNCTION public.reserve_alert(text, text, text, timestamptz, integer) TO coupon_bot;
GRANT EXECUTE ON FUNCTION public.finish_alert(text, text, text, boolean, timestamptz) TO coupon_bot;
```

إذا كان الدور موجودًا مسبقًا، لا تكرر `CREATE ROLE`؛ عيّن كلمة المرور وطبّق أوامر `GRANT` فقط.

## نفق SSH المقيّد

يتوقع الـ workflow مستخدم SSH مخصصًا باسم `coupon-tunnel` ومفتاحًا منفصلًا عن مفتاح إدارة السيرفر. يجب تقييد المفتاح ليقبل تمرير منفذ قاعدة البيانات المحلي فقط، باستخدام خيارات `authorized_keys`:

```text
restrict,port-forwarding,permitopen="127.0.0.1:5432" ssh-ed25519 YOUR_PUBLIC_KEY github-actions-coupon-db
```

لا تستخدم مفتاح `ubuntu` الإداري في GitHub Actions. المفتاح المخصص لا يحتاج shell أو صلاحيات sudo. يبقى PostgreSQL مربوطًا بـ `localhost` ويظل منفذ 5432 مغلقًا خارجيًا.

## أسرار GitHub المطلوبة

في المستودع `ahmed111819/item-tracker-ahs1` افتح **Settings → Secrets and variables → Actions → New repository secret** وأضف:

1. `ORACLE_TUNNEL_PRIVATE_KEY`: المفتاح الخاص المطابق للمفتاح المقيّد أعلاه.
2. `DATABASE_URL`: رابط قاعدة البيانات داخل النفق. استخدم كلمة المرور التي عيّنتها للدور، وتجنب الرموز التي تحتاج ترميزًا في الرابط:

   ```text
   postgresql://coupon_bot:YOUR_URL_SAFE_PASSWORD@127.0.0.1:5432/coupon_storage?sslmode=disable
   ```

اترك أسرار `TELEGRAM_BOT_TOKEN` و`TELEGRAM_CHAT_ID` الحالية كما هي. لا تضع أي مفتاح أو كلمة مرور في ملفات المستودع أو في رسالة المحادثة.

## التشغيل

بعد دمج تغيير النفق وإضافة الإعدادات أعلاه:

1. افتح **Actions → Amazon.sa deal scanners → Run workflow**.
2. تشغيل الماسحات يدويًا يكون مع `continuous=false` للفحص مرة واحدة.
3. لتكرارها، فعّل `continuous=true`: تعمل البوتات الثلاثة بالتوازي، ثم يبدأ انتظار 10 دقائق بعد انتهاء الثلاثة، وبعدها تبدأ دورة جديدة.
4. ألغِ التشغيل الجاري لإيقاف التكرار.

## المساحة

تُحدّث الحالات الثلاث في صفوف ثابتة مضغوطة، ولا ينشأ ملف جديد لكل دورة. التخزين الحالي مضغوط بحوالي 20 MB إجمالًا، وتتوفر مساحة قرص تقارب 39 GB على السيرفر عند آخر تحقق.
