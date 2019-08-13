import logging

from django import http
from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect, render  # render：テスト用に追加
from django.urls import reverse, reverse_lazy
from django.utils import six
from django.utils.http import urlquote
from django.utils.translation import ugettext as _
from django.views import generic

from oscar.core.loading import get_class, get_classes, get_model

from . import signals

# テスト用に追記
from django.http import HttpResponse


ShippingAddressForm, ShippingMethodForm, GatewayForm \
    = get_classes('checkout.forms', ['ShippingAddressForm', 'ShippingMethodForm', 'GatewayForm'])
OrderCreator = get_class('order.utils', 'OrderCreator')
UserAddressForm = get_class('address.forms', 'UserAddressForm')
Repository = get_class('shipping.repository', 'Repository')
AccountAuthView = get_class('customer.views', 'AccountAuthView')
RedirectRequired, UnableToTakePayment, PaymentError \
    = get_classes('payment.exceptions', ['RedirectRequired',
                                         'UnableToTakePayment',
                                         'PaymentError'])
UnableToPlaceOrder = get_class('order.exceptions', 'UnableToPlaceOrder')
OrderPlacementMixin = get_class('checkout.mixins', 'OrderPlacementMixin')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
Order = get_model('order', 'Order')
ShippingAddress = get_model('order', 'ShippingAddress')
CommunicationEvent = get_model('order', 'CommunicationEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentEvent = get_model('order', 'PaymentEvent')
UserAddress = get_model('address', 'UserAddress')
Basket = get_model('basket', 'Basket')
Email = get_model('customer', 'Email')
Country = get_model('address', 'Country')
CommunicationEventType = get_model('customer', 'CommunicationEventType')

# Standard logger for checkout events
logger = logging.getLogger('oscar.checkout')


class IndexView(CheckoutSessionMixin, generic.FormView):
    """
    checkout（決済手続き処理）の最初のページ。
    サインインするか、ゲストとして続行するかのどちらかをユーザーに促す。
    ※認証済ユーザでの決済手続きの場合、本ビューは呼ばれない
    """
    template_name = 'checkout/gateway.html'
    form_class = GatewayForm
    success_url = reverse_lazy('checkout:shipping-address')
    pre_conditions = [
        'check_basket_is_not_empty',
        'check_basket_is_valid']

    def get(self, request, *args, **kwargs):
        # We redirect immediately to shipping address stage if the user is
        # signed in.
        if request.user.is_authenticated:
            # We raise a signal to indicate that the user has entered the
            # checkout process so analytics tools can track this event.
            signals.start_checkout.send_robust(
                sender=self, request=request)
            print("checkout_view_IndexView_pass1")
            return self.get_success_response()

        print("checkout_view_IndexView_pass2")
        return super(IndexView, self).get(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(IndexView, self).get_form_kwargs()
        email = self.checkout_session.get_guest_email()
        if email:
            kwargs['initial'] = {
                'username': email,
            }
        print("checkout_view_IndexView_pass3")
        return kwargs

    def form_valid(self, form):
        if form.is_guest_checkout() or form.is_new_account_checkout():
            email = form.cleaned_data['username']
            self.checkout_session.set_guest_email(email)

            # We raise a signal to indicate that the user has entered the
            # checkout process by specifying an email address.
            signals.start_checkout.send_robust(
                sender=self, request=self.request, email=email)

            if form.is_new_account_checkout():
                messages.info(
                    self.request,
                    _("Create your account and then you will be redirected "
                      "back to the checkout process"))
                self.success_url = "%s?next=%s&email=%s" % (
                    reverse('customer:register'),
                    reverse('checkout:shipping-address'),
                    urlquote(email)
                )
        else:
            user = form.get_user()
            login(self.request, user)

            # We raise a signal to indicate that the user has entered the
            # checkout process.
            signals.start_checkout.send_robust(
                sender=self, request=self.request)

        print("checkout_view_IndexView_pass4")
        return redirect(self.get_success_url())

    def get_success_response(self):
        print("checkout_view_IndexView_pass5")
        return redirect(self.get_success_url())


# ================
# SHIPPING ADDRESS
# ================


class ShippingAddressView(CheckoutSessionMixin, generic.FormView):
    """
    注文の配送先住所を決定するビュー

    デフォルトの動作では、ユーザーの住所録から住所のリストが表示され、
    そこからユーザーは配送先住所として1つを選択できる。
    これらのユーザーアドレスは随時追加/編集/削除することができる。
    このアドレスは、ユーザーがチェックアウトすると自動的に配送先住所に変換される。

    代わりに、セッションに保存され(★)、←ユーザ情報の１つとして
    後で注文が正常に送信されたときにShippingAddressモデルとして保存される配送先住所を直接入力することもできます。
    """
    template_name = 'checkout/shipping_address.html'
    form_class = ShippingAddressForm
    success_url = reverse_lazy('checkout:shipping-method')
    pre_conditions = ['check_basket_is_not_empty',
                      'check_basket_is_valid',
                      'check_user_email_is_captured']
    skip_conditions = ['skip_unless_basket_requires_shipping']

    def get_initial(self):
        initial = self.checkout_session.new_shipping_address_fields()
        if initial:
            initial = initial.copy()
            # Convert the primary key stored in the session into a Country
            # instance
            try:
                initial['country'] = Country.objects.get(
                    iso_3166_1_a2=initial.pop('country_id'))
            except Country.DoesNotExist:
                # Hmm, the previously selected Country no longer exists. We
                # ignore this.
                pass

        print("checkout_view_ShippingAddressView_pass1")
        return initial

    def get_context_data(self, **kwargs):
        ctx = super(ShippingAddressView, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated:  # ユーザ認証されたときのみ実行するif節
            # アドレス帳データを検索する
            ctx['addresses'] = self.get_available_addresses()

        print("checkout_view_ShippingAddressView_pass2")
        return ctx

    def get_available_addresses(self):
        # Include only addresses where the country is flagged as valid for
        # shipping. Also, use ordering to ensure the default address comes
        # first.
        print("checkout_view_ShippingAddressView_pass3")
        return self.request.user.addresses.filter(
            country__is_shipping_country=True).order_by(
            '-is_default_for_shipping')

    def post(self, request, *args, **kwargs):
        # Check if a shipping address was selected directly (eg no form was
        # filled in)
        if self.request.user.is_authenticated \
                and 'address_id' in self.request.POST:
            address = UserAddress._default_manager.get(
                pk=self.request.POST['address_id'], user=self.request.user)
            action = self.request.POST.get('action', None)
            if action == 'ship_to':
                # User has selected a previous address to ship to
                self.checkout_session.ship_to_user_address(address)
                print("checkout_view_ShippingAddressView_pass4")
                return redirect(self.get_success_url())
            else:
                print("checkout_view_ShippingAddressView_pass5")
                return http.HttpResponseBadRequest()
        else:
            print("checkout_view_ShippingAddressView_pass6")
            return super(ShippingAddressView, self).post(
                request, *args, **kwargs)

    def form_valid(self, form):
        # アドレスの詳細をセッションに保存して次のステップにリダイレクトする
        address_fields = dict(
            (k, v) for (k, v) in form.instance.__dict__.items()
            if not k.startswith('_'))
        self.checkout_session.ship_to_new_address(address_fields)

        print("checkout_view_ShippingAddressView_pass7")
        return super(ShippingAddressView, self).form_valid(form)


class UserAddressUpdateView(CheckoutSessionMixin, generic.UpdateView):
    """
    Update a user address
    """
    template_name = 'checkout/user_address_form.html'
    form_class = UserAddressForm
    success_url = reverse_lazy('checkout:shipping-address')

    def get_queryset(self):
        print("checkout_view_UserAddressUpdateView_pass1")
        return self.request.user.addresses.all()

    def get_form_kwargs(self):
        kwargs = super(UserAddressUpdateView, self).get_form_kwargs()
        kwargs['user'] = self.request.user
        print("checkout_view_UserAddressUpdateView_pass2")
        return kwargs

    def get_success_url(self):
        messages.info(self.request, _("Address saved"))
        print("checkout_view_UserAddressUpdateView_pass3")
        return super(UserAddressUpdateView, self).get_success_url()


class UserAddressDeleteView(CheckoutSessionMixin, generic.DeleteView):
    """
    Delete an address from a user's address book.
    """
    template_name = 'checkout/user_address_delete.html'
    success_url = reverse_lazy('checkout:shipping-address')

    def get_queryset(self):
        print("checkout_view_UserAddressDeleteView_pass1")
        return self.request.user.addresses.all()

    def get_success_url(self):
        messages.info(self.request, _("Address deleted"))
        print("checkout_view_UserAddressDeleteView_pass2")
        return super(UserAddressDeleteView, self).get_success_url()


# ===============
# 配送方法
# ===============



# CheckoutSessionMixinクラス、FormViewクラスを継承
class ShippingMethodView(CheckoutSessionMixin, generic.FormView):
    """
    ユーザーが配送方法を選択できるようにするためのビュー

    配送方法は主にドメイン固有であるため、このビューは通常サブクラス化してカスタマイズする必要がある

    The default behaviour is to load all the available shipping methods
    using the shipping Repository.  If there is only 1, then it is
    automatically selected.  Otherwise, a page is rendered where
    the user can choose the appropriate one.
    """
    template_name = 'checkout/shipping_methods.html'
    form_class = ShippingMethodForm

    # 継承クラスの変数をオーバーライド
    pre_conditions = ['check_basket_is_not_empty',
                      'check_basket_is_valid',
                      'check_user_email_is_captured']

    def post(self, request, *args, **kwargs):
        self._methods = self.get_available_shipping_methods()
        print("checkout_view_ShippingMethodView_pass1")
        return super(ShippingMethodView, self).post(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        # These pre-conditions can't easily be factored out into the normal
        # pre-conditions as they do more than run a test and then raise an
        # exception on failure.

        # Check that shipping is required at all
        if not request.basket.is_shipping_required():
            # No shipping required - we store a special code to indicate so.
            self.checkout_session.use_shipping_method(
                NoShippingRequired().code)
            print("checkout_view_ShippingMethodView_pass2")
            return self.get_success_response()

        # Check that shipping address has been completed
        if not self.checkout_session.is_shipping_address_set():
            messages.error(request, _("Please choose a shipping address"))
            print("checkout_view_ShippingMethodView_pass3")
            return redirect('checkout:shipping-address')

        # Save shipping methods as instance var as we need them both here
        # and when setting the context vars.
        self._methods = self.get_available_shipping_methods()
        if len(self._methods) == 0:
            # No shipping methods available for given address
            messages.warning(request, _(
                "Shipping is unavailable for your chosen address - please "
                "choose another"))
            print("checkout_view_ShippingMethodView_pass4")
            return redirect('checkout:shipping-address')
        elif len(self._methods) == 1:
            # Only one shipping method - set this and redirect onto the next
            # step
            self.checkout_session.use_shipping_method(self._methods[0].code)
            print("checkout_view_ShippingMethodView_pass5")
            return self.get_success_response()

        # Must be more than one available shipping method, we present them to
        # the user to make a choice.

        print("checkout_view_ShippingMethodView_pass6")
        return super(ShippingMethodView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        '''
        処理概要
            ①スーパークラスオブジェクト保持
            ②保持したオブジェクトに新規事項追加
            ③上位クラスのオブジェクトを上書き保存して返却
        '''

        kwargs = super(ShippingMethodView, self).get_context_data(**kwargs)             # super()：python2系の書き方→super(自身のクラス).<メソッド名>←上位クラスのメソッド呼び出し/返却
        kwargs['methods'] = self._methods
        print("checkout_view_ShippingMethodView_pass7")
        return kwargs

    def get_form_kwargs(self):
        kwargs = super(ShippingMethodView, self).get_form_kwargs()
        kwargs['methods'] = self._methods
        print("checkout_view_ShippingMethodView_pass8")
        return kwargs

    def get_available_shipping_methods(self):
        """
        Returns all applicable shipping method objects for a given basket.
        """
        # Shipping methods can depend on the user, the contents of the basket
        # and the shipping address (so we pass all these things to the
        # repository).  I haven't come across a scenario that doesn't fit this
        # system.
        print("checkout_view_ShippingMethodView_pass9")
        return Repository().get_shipping_methods(
            basket=self.request.basket, user=self.request.user,
            shipping_addr=self.get_shipping_address(self.request.basket),
            request=self.request)

    def form_valid(self, form):
        # Save the code for the chosen shipping method in the session
        # and continue to the next step.
        self.checkout_session.use_shipping_method(form.cleaned_data['method_code'])
        print("checkout_view_ShippingMethodView_pass10")
        return self.get_success_response()

    def form_invalid(self, form):
        messages.error(self.request, _("Your submitted shipping method is not"
                                       " permitted"))
        print("checkout_view_ShippingMethodView_pass11")
        return super(ShippingMethodView, self).form_invalid(form)

    def get_success_response(self):
        print("checkout_view_ShippingMethodView_pass12")
        return redirect('checkout:payment-method')


# ==============
# Payment method
# ==============


class PaymentMethodView(CheckoutSessionMixin, generic.TemplateView):
    """
    View for a user to choose which payment method(s) they want to use.

    This would include setting allocations if payment is to be split
    between multiple sources. It's not the place for entering sensitive details
    like bankcard numbers though - that belongs on the payment details view.
    """
    pre_conditions = [
        'check_basket_is_not_empty',
        'check_basket_is_valid',
        'check_user_email_is_captured',
        'check_shipping_data_is_captured']
    skip_conditions = ['skip_unless_payment_is_required']

    def get(self, request, *args, **kwargs):
        # By default we redirect straight onto the payment details view. Shops
        # that require a choice of payment method may want to override this
        # method to implement their specific logic.
        print("checkout_view_PaymentMethodView_pass1")
        return self.get_success_response()

    def get_success_response(self):
        print("checkout_view_PaymentMethodView_pass2")
        return redirect('checkout:payment-details')


# ================
# Order submission
# ================


class PaymentDetailsView(OrderPlacementMixin, generic.TemplateView):
    """
    支払いの詳細を確認して注文を作成する

    このビュークラスは、「payment-details」と「preview」の2つの別々のURLで使用されます。
    `preview`クラス属性はどちらが使われているかを区別するために使われます。
    時系列的に、 `payment-details`（preview = False）は` preview`（preview = True）の前に来ます。

    If sensitive details are required (eg a bankcard), then the payment details
    view should submit to the preview URL and a custom implementation of
    `validate_payment_submission` should be provided.

    - フォームデータが有効な場合、プレビューテンプレートは、支払い詳細フォームを非表示のdiv内に再レンダリングされた状態でレンダリングされるため、
    [注文する]ボタンをクリックしたときに再送信できます。
    これにより、処理中に機密データをディスクのどこかに書き込む必要がなくなります。
    これは `render_preview`を呼び出して追加のテンプレートコンテキストを渡すことで実現できます。

    - フォームデータが無効な場合は、支払い詳細テンプレートを関連するエラーメッセージを付けて再レンダリングする必要があります。
    これは `render_payment_details`を呼び出し、テンプレートに渡すためのフォームインスタンスを渡すことで行うことができます。

    このクラスは意図的に細粒度のメソッドに分割され、ただ1つのことに責任があります。
    これは、機能の1つのコンポーネントだけをサブクラス化してオーバーライドしやすくするためです。

    デフォルトでは支払いが行われないため、すべてのプロジェクトでこのクラスをサブクラス化してカスタマイズする必要があります。
    """

    template_name = 'checkout/payment_details.html'
    template_name_preview = 'checkout/preview.html'

    # これらの条件は、「プレビュー」モードかそうでないかによって、実行時に拡張される
    pre_conditions = [
        'check_basket_is_not_empty',
        'check_basket_is_valid',
        'check_user_email_is_captured',
        'check_shipping_data_is_captured']


    preview = False                                                                 # preview = Trueの場合、すべての注文詳細を提示できるように表示されたプレビューテンプレートをレンダリングする

    def get_pre_conditions(self, request):
        if self.preview:                                                            # プレビュー画面では、支払い情報が正しくキャプチャされたことを確認する必要がある
            return self.pre_conditions + ['check_payment_data_is_captured']
        print("detailview1")
        return super(PaymentDetailsView, self).get_pre_conditions(request)

    def get_skip_conditions(self, request):
        if not self.preview:
            # Payment details should only be collected if necessary
            print("detailview6")
            return ['skip_unless_payment_is_required']
        print("detailview2")
        return super(PaymentDetailsView, self).get_skip_conditions(request)         # []が返される

    def post(self, request, *args, **kwargs):
        # Posting to payment-details isn't the right thing to do.  Form
        # submissions should use the preview URL.
        if not self.preview:
            print("detailview3")
            return http.HttpResponseBadRequest()

        # We use a custom parameter to indicate if this is an attempt to place
        # an order (normally from the preview page).  Without this, we assume a
        # payment form is being submitted from the payment details view. In
        # this case, the form needs validating and the order preview shown.
        if request.POST.get('action', '') == 'place_order':
            print("detailview4")
            return self.handle_place_order_submission(request)
        print("detailview5")
        return self.handle_payment_details_submission(request)

    def handle_place_order_submission(self, request):
        """
        Handle a request to place an order.

        This method is normally called after the customer has clicked "place
        order" on the preview page. It's responsible for (re-)validating any
        form information then building the submission dict to pass to the
        `submit` method.

        If forms are submitted on your payment details view, you should
        override this method to ensure they are valid before extracting their
        data into the submission dict and passing it onto `submit`.
        """
        print("detailview7")
        return self.submit(**self.build_submission())

    def handle_payment_details_submission(self, request):
        """
        Handle a request to submit payment details.

        This method will need to be overridden by projects that require forms
        to be submitted on the payment details view.  The new version of this
        method should validate the submitted form data and:

        - If the form data is valid, show the preview view with the forms
          re-rendered in the page
        - If the form data is invalid, show the payment details view with
          the form errors showing.

        """
        # No form data to validate by default, so we simply render the preview
        # page.  If validating form data and it's invalid, then call the
        # render_payment_details view.
        print("detailview8")
        return self.render_preview(request)

    def render_preview(self, request, **kwargs):
        """
        注文のプレビューを表示する。

        If sensitive data was submitted on the payment details page, you will
        need to pass it back to the view here so it can be stored in hidden
        form inputs.  This avoids ever writing the sensitive data to disk.
        """
        self.preview = True
        ctx = self.get_context_data(**kwargs)
        print("detailview9")
        return self.render_to_response(ctx)

    def render_payment_details(self, request, **kwargs):
        """
        Show the payment details page

        This method is useful if the submission from the payment details view
        is invalid and needs to be re-rendered with form errors showing.
        """
        self.preview = False
        ctx = self.get_context_data(**kwargs)
        print("detailview10")
        return self.render_to_response(ctx)

    def get_default_billing_address(self):
        """
        Return default billing address for user

        This is useful when the payment details view includes a billing address
        form - you can use this helper method to prepopulate the form.

        Note, this isn't used in core oscar as there is no billing address form
        by default.
        """
        if not self.request.user.is_authenticated:
            print("detailview11")
            return None
        try:
            print("detailview12")
            return self.request.user.addresses.get(is_default_for_billing=True)
        except UserAddress.DoesNotExist:
            print("detailview13")
            return None

    def submit(self, user, basket, shipping_address, shipping_method,  # noqa (too complex (10))
               shipping_charge, billing_address, order_total,
               payment_kwargs=None, order_kwargs=None):
        """
        Submit a basket for order placement.

        The process runs as follows:

         * Generate an order number
         * Freeze the basket so it cannot be modified any more (important when
           redirecting the user to another site for payment as it prevents the
           basket being manipulated during the payment process).
         * Attempt to take payment for the order
           - If payment is successful, place the order
           - If a redirect is required (eg PayPal, 3DSecure), redirect
           - If payment is unsuccessful, show an appropriate error message

        :basket: The basket to submit.
        :payment_kwargs: Additional kwargs to pass to the handle_payment
                         method. It normally makes sense to pass form
                         instances (rather than model instances) so that the
                         forms can be re-rendered correctly if payment fails.
        :order_kwargs: Additional kwargs to pass to the place_order method
        """
        if payment_kwargs is None:
            payment_kwargs = {}
        if order_kwargs is None:
            order_kwargs = {}

        # Taxes must be known at this point
        assert basket.is_tax_known, (
            "Basket tax must be set before a user can place an order")
        assert shipping_charge.is_tax_known, (
            "Shipping charge tax must be set before a user can place an order")

        # We generate the order number first as this will be used
        # in payment requests (ie before the order model has been
        # created).  We also save it in the session for multi-stage
        # checkouts (eg where we redirect to a 3rd party site and place
        # the order on a different request).
        order_number = self.generate_order_number(basket)
        self.checkout_session.set_order_number(order_number)
        logger.info("Order #%s: beginning submission process for basket #%d",
                    order_number, basket.id)

        # Freeze the basket so it cannot be manipulated while the customer is
        # completing payment on a 3rd party site.  Also, store a reference to
        # the basket in the session so that we know which basket to thaw if we
        # get an unsuccessful payment response when redirecting to a 3rd party
        # site.
        self.freeze_basket(basket)
        self.checkout_session.set_submitted_basket(basket)

        # We define a general error message for when an unanticipated payment
        # error occurs.
        error_msg = _("A problem occurred while processing payment for this "
                      "order - no payment has been taken.  Please "
                      "contact customer services if this problem persists")

        signals.pre_payment.send_robust(sender=self, view=self)

        try:
            self.handle_payment(order_number, order_total, **payment_kwargs)
        except RedirectRequired as e:
            # Redirect required (eg PayPal, 3DS)
            logger.info("Order #%s: redirecting to %s", order_number, e.url)
            print("detailview14")
            return http.HttpResponseRedirect(e.url)
        except UnableToTakePayment as e:
            # Something went wrong with payment but in an anticipated way.  Eg
            # their bankcard has expired, wrong card number - that kind of
            # thing. This type of exception is supposed to set a friendly error
            # message that makes sense to the customer.
            msg = six.text_type(e)
            logger.warning(
                "Order #%s: unable to take payment (%s) - restoring basket",
                order_number, msg)
            self.restore_frozen_basket()

            # We assume that the details submitted on the payment details view
            # were invalid (eg expired bankcard).
            print("detailview15")
            return self.render_payment_details(
                self.request, error=msg, **payment_kwargs)
        except PaymentError as e:
            # A general payment error - Something went wrong which wasn't
            # anticipated.  Eg, the payment gateway is down (it happens), your
            # credentials are wrong - that king of thing.
            # It makes sense to configure the checkout logger to
            # mail admins on an error as this issue warrants some further
            # investigation.
            msg = six.text_type(e)
            logger.error("Order #%s: payment error (%s)", order_number, msg,
                         exc_info=True)
            self.restore_frozen_basket()
            print("detailview16")
            return self.render_preview(
                self.request, error=error_msg, **payment_kwargs)
        except Exception as e:
            # Unhandled exception - hopefully, you will only ever see this in
            # development...
            logger.error(
                "Order #%s: unhandled exception while taking payment (%s)",
                order_number, e, exc_info=True)
            self.restore_frozen_basket()
            print("detailview17")
            return self.render_preview(
                self.request, error=error_msg, **payment_kwargs)

        signals.post_payment.send_robust(sender=self, view=self)

        # If all is ok with payment, try and place order
        logger.info("Order #%s: payment successful, placing order",
                    order_number)
        try:
            print("detailview18")
            return self.handle_order_placement(
                order_number, user, basket, shipping_address, shipping_method,
                shipping_charge, billing_address, order_total, **order_kwargs)
        except UnableToPlaceOrder as e:
            # It's possible that something will go wrong while trying to
            # actually place an order.  Not a good situation to be in as a
            # payment transaction may already have taken place, but needs
            # to be handled gracefully.
            msg = six.text_type(e)
            logger.error("Order #%s: unable to place order - %s",
                         order_number, msg, exc_info=True)
            self.restore_frozen_basket()
            print("detailview19")
            return self.render_preview(
                self.request, error=msg, **payment_kwargs)

    def get_template_names(self):
        print("detailview20")
        return [self.template_name_preview] if self.preview else [
            self.template_name]


# =========
# Thank you
# =========


# 仮画面生成
# def thanks(request):
#     if request.method == 'GET':
#         context = {'message': "thanks!",}
#
#         return render(request, 'sample.html', context)

# テスト用仮画面作成
# def thanks(request):
#     return HttpResponse("thank you!")


# 「ありがとう」画面を出すときのセッション管理も処理として実施している
class ThankYouView(generic.DetailView):
    """
    送信したばかりの注文を要約した「ありがとう」ページを表示する。
    """
    template_name = 'checkout/thank_you.html'
    context_object_name = 'order'

    def get_object(self):
        # スーパーユーザーがテストのために注文完了/感謝ページを強制することを可能にする
        order = None
        if self.request.user.is_superuser:                              # リクエストしたユーザがスーパーユーザだった場合
            if 'order_number' in self.request.GET:
                order = Order._default_manager.get(
                    number=self.request.GET['order_number'])
            elif 'order_id' in self.request.GET:
                order = Order._default_manager.get(
                    id=self.request.GET['order_id'])

        if not order:
            if 'checkout_order_id' in self.request.session:         # セッションの中に「checkout_order_id」キーに紐付くデータがあった場合
                order = Order._default_manager.get(
                    pk=self.request.session['checkout_order_id'])   # Orderテーブルから「（例）pk＝8」のデータをオブジェクトとして取得
            else:
                raise http.Http404(_("No order found"))     # http.Http404()←例外画面のテンプレートを出力するメソッド。中に記述する文字を引数に指定

        print("checkout_view_ThankYouView_pass1")
        return order

    def get_context_data(self, *args, **kwargs):
        # ctx = super(ThankYouView, self).get_context_data(*args, **kwargs)  # ←python2系の書き方
        ctx = super().get_context_data(*args, **kwargs)

        key = 'order_{}_thankyou_viewed'.format(ctx['order'].pk)

        if not self.request.session.get(key, False):
            self.request.session[key] = True
            ctx['send_analytics_event'] = True
        else:
            ctx['send_analytics_event'] = False


        print("checkout_view_ThankYouView_pass2")
        return ctx
