# تشغيل بوت الكوبونات الواحد على GitHub مع PostgreSQL

هذه الحزمة تنقل تخزين Bot 1 إلى PostgreSQL على السيرفر. الفحص وإرسال Telegram يبقيان في GitHub Actions. لن تعمل تلقائيًا قبل إعداد الاتصال واستيراد الملفات القديمة.

## محتويات الحزمة

- `postgres_schema.sql`: ينشئ جداول الحالة وسجل التنبيهات ودوال الحجز الذري.
- `postgres_storage.py`: وظائف قراءة/حفظ الحالة وإدارة منع تكرار التنبيه.
- `import_postgres.py`: استيراد أولي لملف تتبع البوت وملف التنبيهات.
- `requirements-postgres.txt`: مكتبة PostgreSQL لأداة الاستيراد المحلية.

## إعداد قاعدة البيانات على السيرفر

1. أنشئ قاعدة PostgreSQL ومستخدمًا مخصصًا باسم `coupon_bot` بكلمة مرور قوية.
2. شغّل `postgres_schema.sql` مرة واحدة بحساب مالك قاعدة البيانات.
3. امنح المستخدم `coupon_bot` الصلاحيات التالية، مع استبدال `YOUR_DATABASE` باسم القاعدة:

   ```sql
   GRANT CONNECT ON DATABASE YOUR_DATABASE TO coupon_bot;
   GRANT USAGE ON SCHEMA public TO coupon_bot;
   GRANT SELECT, INSERT, UPDATE ON public.bot_state TO coupon_bot;
   GRANT SELECT, INSERT, UPDATE, DELETE ON public.alert_registry TO coupon_bot;
   GRANT SELECT, INSERT ON public.storage_migrations TO coupon_bot;
   GRANT EXECUTE ON FUNCTION public.reserve_alert(text, text, text, timestamptz, integer) TO coupon_bot;
   GRANT EXECUTE ON FUNCTION public.finish_alert(text, text, text, boolean, timestamptz) TO coupon_bot;
   ```

4. فعّل اتصال TLS، ولا تفتح منفذ PostgreSQL للإنترنت بلا قيود. GitHub Actions يحتاج مسارًا آمنًا للوصول للسيرفر؛ لا تضع كلمة المرور في الكود أو في المستودع العام.

## استيراد البيانات القديمة مرة واحدة

يحتاج البوت ملفين مختلفين:

- `amazon_deals_tracking_bot1.json`: سجل المنتجات والأسعار وحالة الفحص.
- `amazon_deals_sent_registry.json`: سجل إشعارات Telegram المرسلة.

الملف المرفق حاليًا هو سجل الإشعارات. لا يحتوي سجل المنتجات؛ يلزم الحصول على `amazon_deals_tracking_bot1.json` من نسخة Bot 1 القديمة قبل التشغيل حتى لا يبدأ تاريخ الأسعار من الصفر.

من جهاز موثوق يحتفظ بالملفين، شغّل:

```powershell
$env:DATABASE_URL = 'postgresql://coupon_bot:YOUR_PASSWORD@YOUR_SERVER:5432/YOUR_DATABASE?sslmode=require'
python -m pip install -r requirements-postgres.txt
python import_postgres.py --tracking 'amazon_deals_tracking_bot1.json' --registry 'amazon_deals_sent_registry.json' --bot-id bot1
```

الاستيراد آمن لإعادة المحاولة: لا يستبدل صف حالة موجودًا ولا يكتب فوق مفاتيح تنبيه موجودة. لا ترفع ملفي JSON إلى المستودع العام.

## GitHub Actions

بعد اكتمال إعداد السيرفر والاستيراد:

1. أضف Secret باسم `DATABASE_URL` في إعدادات مستودع GitHub. قيمة السر هي رابط PostgreSQL الكامل مع `sslmode=require`.
2. تأكد من بقاء `TELEGRAM_BOT_TOKEN` و`TELEGRAM_CHAT_ID` كما هما.
3. شغّل Workflow باسم **Amazon.sa coupon bot** مع خيار `continuous` مفعّل. البوت ينفذ فحصًا كاملًا، ينتظر 10 دقائق بعد انتهائه، ثم يبدأ دورة جديدة. أوقفه بإلغاء التشغيل الجاري.

التغييرات تستهدف بوتًا واحدًا فقط، ولا تشغّل Bot 2 أو Bot 3. لا تبدأ التشغيل قبل استيراد الحالتين؛ إذا غاب أحدهما يتوقف البوت برسالة خطأ بدل إنشاء سجل فارغ.

## التخزين والمساحة

تُخزّن حالة Bot 1 كـ JSON مضغوط في صف واحد اسمه `bot1`. كل حفظ يستبدل قيمة الصف نفسه بدل إنشاء ملف مؤرخ جديد. حجم نسخة Bot 1 المضغوطة في تشغيل سابق كان نحو 8.5 MB؛ اترك مساحة إضافية لـ WAL والنسخ الاحتياطية والتنظيف الدوري في PostgreSQL.

كل checkpoint ما زال ينقل الحالة المضغوطة بين GitHub والسيرفر. هذا يوقف الاعتماد على حصة تخزين Supabase، لكنه لا يحول الحفظ إلى تحديثات صغيرة؛ تحسين النقل لاحقًا يتطلب تخزين المنتجات كسجلات محدثة تدريجيًا.

