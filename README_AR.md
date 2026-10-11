# item-tracker-ahs1 — ماسح عروض Amazon.sa

هذا المستودع عام؛ لا تضف إليه رموز تيليجرام أو مفاتيح Google أو ملفات التتبع. تم إيقاف سير عمل Amazon Yalla في GitHub لأن تشغيله مستضاف خارجيًا.

## البوتات الثلاثة

يشغّل سير العمل Bot 1 وBot 2 وBot 3 بالتوازي على عامل GitHub Actions واحد. لكل بوت روابطه وملف تتبعه المستقل، ويشتركون في سجل التنبيهات على Google Drive. في التشغيل المستمر، يعيد كل بوت الفحص بعد 10 دقائق من انتهاء فحصه، مع إنشاء تشغيل مجدول جديد كل 3 ساعات.

- فحص واحد يدويًا: Actions → Amazon.sa deal scanners → Run workflow، واترك خيار continuous غير محدد.
- تشغيل مستمر: اختر continuous يدويًا، أو اترك الجدولة تبدأ كل 3 ساعات. تنتهي كل مهمة تلقائيًا خلال 3 ساعات كحد أقصى، وتُحفظ نقطة التقدم في Drive لاستئنافها في التشغيل التالي.
- يحتاج مجلد Drive إلى ملفات التتبع الثلاثة: `amazon_deals_tracking_bot1.json` و`amazon_deals_tracking_bot2.json` و`amazon_deals_tracking_bot3.json`، إضافة إلى `amazon_deals_sent_registry.json`.
- لا يحتاج الماسح إلى `state.json` أو مجلد جلسة Amazon.

أوقف نسخ Colab للبوتات الثلاثة عند نقلها للتشغيل في GitHub؛ لا تشغّل أي نسخة Colab بالتزامن مع نسخة GitHub على ملف التتبع نفسه.
