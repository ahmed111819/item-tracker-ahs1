# إعداد Amazon.sa deal scanners مع PostgreSQL

هذه الحزمة تحافظ على اسم Workflow **Amazon.sa deal scanners** وتشغّل البوتات الثلاثة بالتوازي على GitHub Actions. PostgreSQL على السيرفر للتخزين فقط، ولا يشغّل فحص Amazon.

## الملفات

- `postgres_schema.sql`: جداول حالة البوتات وسجل التنبيهات المشترك.
- `postgres_storage.py`: وظائف قراءة/حفظ كل حالة وحجز التنبيه ذريًا.
- `import_postgres.py`: استيراد أولي لملفات التتبع الثلاثة وسجل التنبيهات.
- `requirements-postgres.txt`: مكتبة PostgreSQL لأداة الاستيراد.

## إعداد قاعدة البيانات

1. أنشئ قاعدة PostgreSQL على السيرفر ومستخدمًا مخصصًا باسم `coupon_bot` وكلمة مرور قوية.
2. شغّل `postgres_schema.sql` مرة واحدة بحساب مالك قاعدة البيانات.
3. امنح `coupon_bot` الصلاحيات التالية، مع استبدال `YOUR_DATABASE` باسم القاعدة:

   ```sql
   GRANT CONNECT ON DATABASE YOUR_DATABASE TO coupon_bot;
   GRANT USAGE ON SCHEMA public TO coupon_bot;
   GRANT SELECT, INSERT, UPDATE ON public.bot_state TO coupon_bot;
   GRANT SELECT, INSERT, UPDATE, DELETE ON public.alert_registry TO coupon_bot;
   GRANT SELECT, INSERT ON public.storage_migrations TO coupon_bot;
   GRANT EXECUTE ON FUNCTION public.reserve_alert(text, text, text, timestamptz, integer) TO coupon_bot;
   GRANT EXECUTE ON FUNCTION public.finish_alert(text, text, text, boolean, timestamptz) TO coupon_bot;
   ```

4. فعّل TLS ولا تفتح PostgreSQL للإنترنت بلا قيود. يجب أن يصل GitHub Actions للسيرفر عبر اتصال آمن. لا تضع كلمة المرور في الكود أو المستودع العام.

## استيراد الحالة القديمة قبل التشغيل

يلزم وجود أربعة ملفات:

- `amazon_deals_tracking_bot1.json`
- `amazon_deals_tracking_bot2.json`
- `amazon_deals_tracking_bot3.json`
- `amazon_deals_sent_registry.json`

ملف JSON الذي أرسلته هو سجل التنبيهات فقط. لا يحتوي سجلات المنتجات والأسعار؛ نحتاج ملفات التتبع الثلاثة من مصدرها القديم. لا تشغّل البوت قبل استيرادها، لأن الحالة الفارغة قد تسبب تكرار التنبيهات أو فقدان تاريخ الأسعار. الكود يرفض التشغيل إذا غاب أي صف أو سجل التنبيهات.

من جهاز موثوق يحتوي الملفات الأربعة:

```powershell
$env:DATABASE_URL = 'postgresql://coupon_bot:YOUR_PASSWORD@YOUR_SERVER:5432/YOUR_DATABASE?sslmode=require'
python -m pip install -r requirements-postgres.txt
python import_postgres.py --tracking "bot1=amazon_deals_tracking_bot1.json" --tracking "bot2=amazon_deals_tracking_bot2.json" --tracking "bot3=amazon_deals_tracking_bot3.json" --registry "amazon_deals_sent_registry.json"
```

الاستيراد يمكن إعادة محاولته؛ لا يستبدل صف حالة موجودًا ولا يكتب فوق مفاتيح تنبيه موجودة. لا ترفع ملفات JSON إلى GitHub.

## GitHub Actions

بعد إعداد السيرفر واستيراد الملفات:

1. أضف GitHub Actions Secret باسم `DATABASE_URL`، مع رابط PostgreSQL و`sslmode=require`.
2. أبقِ `TELEGRAM_BOT_TOKEN` و`TELEGRAM_CHAT_ID` في GitHub Secrets.
3. شغّل Workflow **Amazon.sa deal scanners** مع تفعيل `continuous`. تبدأ البوتات الثلاثة معًا، وبعد انتهاء الثلاثة ينتظر Workflow 10 دقائق ثم يشغّل دورة جديدة.
4. لإيقاف التكرار ألغِ التشغيل الجاري.

## المساحة

كل بوت له صف حالة مضغوط ثابت (`bot1` و`bot2` و`bot3`) يعاد تحديثه بدل إنشاء ملف جديد. أحجام الملفات المضغوطة السابقة كانت تقريبًا 8.5 MB لكل بوت؛ احجز حوالي 30 MB للحالة الحالية مع مساحة أكبر لـ PostgreSQL WAL والنسخ الاحتياطية والتنظيف التلقائي.

كل checkpoint ما زال ينقل الحالة المضغوطة بين GitHub Actions والسيرفر. هذا يرفع حد التخزين عن Supabase، لكنه لا يلغي نقل الحالة. يمكن تقليل النقل لاحقًا بتحويل البيانات إلى تحديثات منتجات متفرقة بدل رفع ملف الحالة كاملًا.

