from decimal import Decimal as D

from django.conf import settings
from django.contrib.sites.models import Site
from django.db import transaction
from django.utils.translation import ugettext_lazy as _

from oscar.apps.order.signals import order_placed
from oscar.core.loading import get_model

from . import exceptions

Order = get_model('order', 'Order')
Line = get_model('order', 'Line')
OrderDiscount = get_model('order', 'OrderDiscount')

# 「Place Order(注文する)」ボタン押下時に動作する
class OrderNumberGenerator(object):
    """
    注文番号を生成するクラス。

    注文番号は支払い時にまれに必要とされる。
    これはモデル→注文テーブルが新規作成される前に実行される。
    """

    def order_number(self, basket):
        """
        特定のカートの注文番号を返す
        """
        return 100000 + basket.id


class OrderCreator(object):
    """
    さまざまなモデルを書き出して注文を生成するクラス
    """

    def place_order(self, basket, total,  # noqa (too complex (12))
                    shipping_method, shipping_charge, user=None,
                    shipping_address=None, billing_address=None,
                    order_number=None, status=None, request=None, **kwargs):
        """
        注文するには、バスケットとセッションのデータに基づいてすべての関連モデルを作成する。
        """

        print("place_order_1")
        if basket.is_empty:
            raise ValueError(_("Empty baskets cannot be submitted"))
        if not order_number:
            generator = OrderNumberGenerator()
            order_number = generator.order_number(basket)
        if not status and hasattr(settings, 'OSCAR_INITIAL_ORDER_STATUS'):
            status = getattr(settings, 'OSCAR_INITIAL_ORDER_STATUS')

        if Order._default_manager.filter(number=order_number).exists():
            raise ValueError(_("There is already an order with number %s")
                             % order_number)

        with transaction.atomic():

            # Ok - everything seems to be in order, let's place the order
            order = self.create_order_model(
                user, basket, shipping_address, shipping_method, shipping_charge,
                billing_address, total, order_number, status, request, **kwargs)
            for line in basket.all_lines():
                self.create_line_models(order, line)
                self.update_stock_records(line)

            for voucher in basket.vouchers.select_for_update():
                available_to_user, msg = voucher.is_available_to_user(user=user)
                if not voucher.is_active() or not available_to_user:
                    raise ValueError(msg)

            # Record any discounts associated with this order
            for application in basket.offer_applications:
                # Trigger any deferred benefits from offers and capture the
                # resulting message
                application['message'] \
                    = application['offer'].apply_deferred_benefit(basket, order,
                                                                  application)
                # Record offer application results
                if application['result'].affects_shipping:
                    # Skip zero shipping discounts
                    shipping_discount = shipping_method.discount(basket)
                    if shipping_discount <= D('0.00'):
                        continue
                    # If a shipping offer, we need to grab the actual discount off
                    # the shipping method instance, which should be wrapped in an
                    # OfferDiscount instance.
                    application['discount'] = shipping_discount
                self.create_discount_model(order, application)
                self.record_discount(application)

            for voucher in basket.vouchers.all():
                self.record_voucher_usage(order, voucher, user)

        # Send signal for analytics to pick up
        order_placed.send(sender=self, order=order, user=user)

        print("place_order_2")
        return order

    def create_order_model(self, user, basket, shipping_address,
                           shipping_method, shipping_charge, billing_address,
                           total, order_number, status, request=None, **extra_order_fields):
        """Create an order model."""
        order_data = {'basket': basket,
                      'number': order_number,
                      'currency': total.currency,
                      'total_incl_tax': total.incl_tax,
                      'total_excl_tax': total.excl_tax,
                      'shipping_incl_tax': shipping_charge.incl_tax,
                      'shipping_excl_tax': shipping_charge.excl_tax,
                      'shipping_method': shipping_method.name,
                      'shipping_code': shipping_method.code}
        if shipping_address:
            order_data['shipping_address'] = shipping_address
        if billing_address:
            order_data['billing_address'] = billing_address
        if user and user.is_authenticated:
            order_data['user_id'] = user.id
        if status:
            order_data['status'] = status
        if extra_order_fields:
            order_data.update(extra_order_fields)
        if 'site' not in order_data:
            order_data['site'] = Site._default_manager.get_current(request)
        order = Order(**order_data)
        order.save()
        print("create_order_model_1")
        return order

    def create_line_models(self, order, basket_line, extra_line_fields=None):
        """
        バッチラインモデルを作成します。

        追加行フィールド値として辞書を渡すことで追加フィールドを設定できます。
        """
        product = basket_line.product
        stockrecord = basket_line.stockrecord
        if not stockrecord:
            raise exceptions.UnableToPlaceOrder(
                "Basket line #%d has no stockrecord" % basket_line.id)
        partner = stockrecord.partner
        line_data = {
            'order': order,
            # Partner details
            'partner': partner,
            'partner_name': partner.name,
            'partner_sku': stockrecord.partner_sku,
            'stockrecord': stockrecord,
            # Product details
            'product': product,
            'title': product.get_title(),
            'upc': product.upc,
            'quantity': basket_line.quantity,
            # Price details
            'line_price_excl_tax':
            basket_line.line_price_excl_tax_incl_discounts,
            'line_price_incl_tax':
            basket_line.line_price_incl_tax_incl_discounts,
            'line_price_before_discounts_excl_tax':
            basket_line.line_price_excl_tax,
            'line_price_before_discounts_incl_tax':
            basket_line.line_price_incl_tax,
            # Reporting details
            'unit_cost_price': stockrecord.cost_price,
            'unit_price_incl_tax': basket_line.unit_price_incl_tax,
            'unit_price_excl_tax': basket_line.unit_price_excl_tax,
            'unit_retail_price': stockrecord.price_retail,
            # Shipping details
            'est_dispatch_date':
            basket_line.purchase_info.availability.dispatch_date
        }
        extra_line_fields = extra_line_fields or {}
        if hasattr(settings, 'OSCAR_INITIAL_LINE_STATUS'):
            if not (extra_line_fields and 'status' in extra_line_fields):
                extra_line_fields['status'] = getattr(
                    settings, 'OSCAR_INITIAL_LINE_STATUS')
        if extra_line_fields:
            line_data.update(extra_line_fields)

        order_line = Line._default_manager.create(**line_data)
        self.create_line_price_models(order, order_line, basket_line)
        self.create_line_attributes(order, order_line, basket_line)
        self.create_additional_line_models(order, order_line, basket_line)

        print("create_line_models_1")
        return order_line

    def update_stock_records(self, line):
        """
        このオーダー明細に関連する在庫レコードを更新します
        """
        if line.product.get_product_class().track_stock:
            print("update_stock_records_1")
            line.stockrecord.allocate(line.quantity)

    def create_additional_line_models(self, order, order_line, basket_line):
        """
        オーバーライドされるように設計された空のメソッド。

        いくつかのアプリケーションは線に関する追加情報を必要とします、
        このメソッドは与えられた線に関連する追加モデルを作成するためのきれいな場所を提供します。
        """
        pass

    def create_line_price_models(self, order, order_line, basket_line):
        """
        バッチライン価格モデルを作成する
        """
        breakdown = basket_line.get_price_breakdown()
        for price_incl_tax, price_excl_tax, quantity in breakdown:
            order_line.prices.create(
                order=order,
                quantity=quantity,
                price_incl_tax=price_incl_tax,
                price_excl_tax=price_excl_tax)

    def create_line_attributes(self, order, order_line, basket_line):
        """
        バッチライン属性を作成する
        """
        for attr in basket_line.attributes.all():
            order_line.attributes.create(
                option=attr.option,
                type=attr.option.code,
                value=attr.value)

    def create_discount_model(self, order, discount):

        """
        バスケットに添付されている各オファーアプリケーションの注文割引モデルを作成する
        """
        order_discount = OrderDiscount(
            order=order,
            message=discount['message'] or '',
            offer_id=discount['offer'].id,
            frequency=discount['freq'],
            amount=discount['discount'])
        result = discount['result']
        if result.affects_shipping:
            order_discount.category = OrderDiscount.SHIPPING
        elif result.affects_post_order:
            order_discount.category = OrderDiscount.DEFERRED
        voucher = discount.get('voucher', None)
        if voucher:
            order_discount.voucher_id = voucher.id
            order_discount.voucher_code = voucher.code
        order_discount.save()

    def record_discount(self, discount):
        discount['offer'].record_usage(discount)
        if 'voucher' in discount and discount['voucher']:
            discount['voucher'].record_discount(discount)

    def record_voucher_usage(self, order, voucher, user):
        """
        このバウチャーを気にするモデルを更新する
        """
        voucher.record_usage(order, user)
