
from django.contrib import admin
from django.urls import path, include
from oscar.app import application
from . import settings

urlpatterns = [
    path('admin/', admin.site.urls),    # admin（管理者）ページへの遷移
    path('', application.urls),
#     path('oscar/', application.urls),         # Oscarアプリ側で設定したURlに遷移

]


# djangoデバッグツール導入
if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [path('__debug__/', include(debug_toolbar.urls))] + urlpatterns


# 以下はOscar構築手順に載っていたもの→無くても動きそうなため、今回は除外→なぜ必要になるのかは別途調査
# path('i18n/', include('django.conf.urls.i18n')),#