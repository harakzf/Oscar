from django.apps import AppConfig
from django.utils.translation import ugettext_lazy as _


class OrderConfig(AppConfig):
    label = 'order'     # app_labelと同等？
    name = 'oscar.apps.order'
#     verbose_name = _('Order')
    verbose_name = '商品注文アプリ'    # adminページに表示されるアプリ名を指定
