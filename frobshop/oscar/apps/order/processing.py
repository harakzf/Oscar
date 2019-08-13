from decimal import Decimal as D

from django.utils.translation import ugettext_lazy as _

from oscar.apps.order import exceptions
from oscar.core.loading import get_model

ShippingEventQuantity = get_model('order', 'ShippingEventQuantity')
PaymentEventQuantity = get_model('order', 'PaymentEventQuantity')

# クラスはこの１つのみ
class EventHandler(object):
    """
    要求された注文イベントを処理する重要なクラス。
    →ショップの注文処理パイプラインのコアロジックを実装している
    """

    def __init__(self, user=None):
        print("pass_order_1")
        self.user = user

    # Core API
    # --------

    def handle_shipping_event(self, order, event_type, lines,
                              line_quantities, **kwargs):
        """
        特定の注文に対する配送イベント（機能）を処理（実装）する。

        This is most common entry point to this class - most of your order
        processing should be modelled around shipping events.  Shipping events
        can be used to trigger payment and communication events.

        You will generally want to override this method to implement the
        specifics of you order processing pipeline.
        """
        # 以下、実装例
        self.validate_shipping_event(                                   # 下のメソッド呼出
            order, event_type, lines, line_quantities, **kwargs)

        print("pass_order_2")
        return self.create_shipping_event(
            order, event_type, lines, line_quantities, **kwargs)

    def handle_payment_event(self, order, event_type, amount, lines=None,
                             line_quantities=None, **kwargs):
        """
        特定の注文に対する支払いイベントを処理する

        These should normally be called as part of handling a shipping event.
        It is rare to call to this method directly.  It does make sense for
        refunds though where the payment event may be unrelated to a particular
        shipping event and doesn't directly correspond to a set of lines.
        """
        self.validate_payment_event(
            order, event_type, amount, lines, line_quantities, **kwargs)

        print("pass_order_3")
        return self.create_payment_event(
            order, event_type, amount, lines, line_quantities, **kwargs)

    def handle_order_status_change(self, order, new_status, note_msg=None):
        """
        要求された注文状況の変更を処理する

        □Parameters
        ----------
        order : model
            モデルクラスで定義されたorderテーブル。

        new_status：

        note_msg：


        □注釈
        ----------
        このメソッドは通常、クライアントコードによって直接呼び出されることはない。
        主なユースケース：注文がキャンセルされたとき
        これは、いくつかの点で、すべてのラインに影響する出荷イベントと見なすことができる。
        """
        print("pass_order_4")
        order.set_status(new_status)
        if note_msg:
            self.create_note(order, note_msg)

    # Validation methods
    # ------------------

    def validate_shipping_event(self, order, event_type, lines,
                                line_quantities, **kwargs):
        """
        Test if the requested shipping event is permitted.

        If not, raise InvalidShippingEvent
        """
        print("pass_order_5")
        errors = []
        for line, qty in zip(lines, line_quantities):
            # The core logic should be in the model.  Ensure you override
            # 'is_shipping_event_permitted' and enforce the correct order of
            # shipping events.
            if not line.is_shipping_event_permitted(event_type, qty):
                msg = _("The selected quantity for line #%(line_id)s is too"
                        " large") % {'line_id': line.id}
                errors.append(msg)
        if errors:
            raise exceptions.InvalidShippingEvent(", ".join(errors))

    def validate_payment_event(self, order, event_type, amount, lines=None,
                               line_quantities=None, **kwargs):

        print("pass_order_6")
        if lines and line_quantities:
            errors = []
            for line, qty in zip(lines, line_quantities):
                if not line.is_payment_event_permitted(event_type, qty):
                    msg = _("The selected quantity for line #%(line_id)s is too"
                            " large") % {'line_id': line.id}
                    errors.append(msg)
            if errors:
                raise exceptions.InvalidPaymentEvent(", ".join(errors))

    # Query methods
    # -------------
    # These are to help determine the status of lines

    def have_lines_passed_shipping_event(self, order, lines, line_quantities,
                                         event_type):
        """
        渡されたレコード/テーブルと数量が、指定された配送イベントを通過したかどうかを確認するメソッド。

        →特定の配送イベントが許可されているかどうかを検証するのにも役立つ。
        (つまり、出荷前に返品することはできない。)
        """
        print("pass_order_7")
        for line, line_qty in zip(lines, line_quantities):
            if line.shipping_event_quantity(event_type) < line_qty:
                return False
        return True

    # 以下、支払系処理を実装したメソッド
    # -------------

    # 「注文する」ボタンを押下しても処理されない・・・
    def calculate_payment_event_subtotal(self, event_type, lines,
                                         line_quantities):
        """
        □概要
        ----------
        渡されたイベントタイプ、合計行数、および合計行数の合計料金を計算する。
        本イベントに対して、請求された以前の価格を考慮する。

        □Parameters
        ----------
        event_type：


        lines：
            レコード単位でのデータの集まり。

        line_quantities：
            レコードごとの数量。


        □注釈
        ----------
        配送料はこの小計には含まれていない。
        配送料を含める場合は、このメソッドをサブクラス化して拡張する必要がある。
        """
        total = D('0.00')                                                       # 合計値を格納する変数「total」に初期値代入

        print("pass_order_8")

        for line, qty_to_consume in zip(lines, line_quantities):                # zip()：複数のイテラブルオブジェクト（リストやタプルなど）の要素を同時に取得して使用可能
            # すでに決済された価格をスキップする必要があります。
            # これはカウンターの負荷を保つことを含みます。

            # このタイプのイベントにこのレコードがいくつ含まれているかを数える。
            qty_to_skip = line.payment_event_quantity(event_type)       # 与えられたタイプの支払いイベントに関与したレコードの数量が返却されるので、その数量を保持


            if qty_to_skip + qty_to_consume > line.quantity:
                raise exceptions.InvalidPaymentEvent

            # Consume prices in order of ID (this is the default but it's
            # better to be explicit)
            qty_consumed = 0
            for price in line.prices.all().order_by('id'):                      # レコードの「価格」カラムを主キーの昇順で取得→for文で１要素ずつ取り出す

                # qty_consumed == qty_to_consumeになったときにfor文終了
                if qty_consumed == qty_to_consume:
                    break

                qty_available = price.quantity - qty_to_skip
                if qty_available <= 0:
                    # Skip the whole quantity of this price instance
                    qty_to_skip -= price.quantity
                else:
                    # Need to account for some of this price instance and
                    # track how many we needed to skip and how many we settled
                    # for.
                    qty_to_include = min(
                        qty_to_consume - qty_consumed, qty_available)
                    total += qty_to_include * price.price_incl_tax
                    # There can't be any left to skip if we've included some in
                    # our total
                    qty_to_skip = 0
                    qty_consumed += qty_to_include
        return total


    # 以下、在庫に関連して実行される処理を実装したメソッド
    # -----

    def are_stock_allocations_available(self, lines, line_quantities):
        """
        Check whether stock records still have enough stock to honour the
        requested allocations.

        Lines whose product doesn't track stock are disregarded, which means
        this method will return True if only non-stock-tracking-lines are
        passed.
        This means you can just throw all order lines to this method, without
        checking whether stock tracking is enabled or not.
        This is okay, as calling consume_stock_allocations() has no effect for
        non-stock-tracking lines.
        """

        print("pass_order_9")
        for line, qty in zip(lines, line_quantities):
            record = line.stockrecord
            if not record:
                return False
            if not record.can_track_allocations:
                continue
            if not record.is_allocation_consumption_possible(qty):
                return False
        return True

    def consume_stock_allocations(self, order, lines=None, line_quantities=None):
        """
        渡されたレコードの在庫配分をモデル（DBテーブル）に対して消費・反映する。
        明細/数量が渡されない場合は、すべての明細に対して行う。
        """
        print("pass_order_10")
        if not lines:
            lines = order.lines.all()
        if not line_quantities:
            line_quantities = [line.quantity for line in lines]

        # 以下、本メソッドのメイン処理
        for line, qty in zip(lines, line_quantities):
            if line.stockrecord:        # 「line」テーブルの「stockrecord」カラム参照→stockrecordはFKのため、他テーブル(「partner」DBの「StockRecord」テーブル)を参照
                line.stockrecord.consume_allocation(qty)

    def cancel_stock_allocations(self, order, lines=None, line_quantities=None):
        """
        渡された明細の在庫割当を取り消す処理。
        明細/数量が渡されない場合は、すべての明細に対して行う。
        """
        print("pass_order_11")
        if not lines:                       # 明細が渡されない場合
            lines = order.lines.all()       # 明細？モデルオブジェクトすべて取得
        if not line_quantities:             # 数量が渡されない場合
            line_quantities = [line.quantity for line in lines] # 取得した全オブジェクトの「数量」カラム部分のみをリスト化

        # 以下、本メソッドのメイン処理
        for line, qty in zip(lines, line_quantities):
            if line.stockrecord:
                line.stockrecord.cancel_allocation(qty)

    # 以下、モデルインスタンス作成メソッド
    # -----------------------

    def create_shipping_event(self, order, event_type, lines, line_quantities,
                              **kwargs):
        '''配送イベント新規作成処理'''

        print("pass_order_12")

        reference = kwargs.get('reference', '')
        event = order.shipping_events.create(
            event_type=event_type, notes=reference)
        try:
            for line, quantity in zip(lines, line_quantities):
                event.line_quantities.create(
                    line=line, quantity=quantity)
        except exceptions.InvalidShippingEvent:
            event.delete()
            raise
        return event

    def create_payment_event(self, order, event_type, amount, lines=None,
                             line_quantities=None, **kwargs):

        print("pass_order_13")
        reference = kwargs.get('reference', "")
        event = order.payment_events.create(
            event_type=event_type, amount=amount, reference=reference)
        if lines and line_quantities:
            for line, quantity in zip(lines, line_quantities):
                event.line_quantities.create(
                    line=line, quantity=quantity)
        return event

    def create_communication_event(self, order, event_type):
        print("pass_order_14")
        return order.communication_events.create(event_type=event_type)

    def create_note(self, order, message, note_type='System'):
        print("pass_order_15")
        return order.notes.create(
            message=message, note_type=note_type, user=self.user)
