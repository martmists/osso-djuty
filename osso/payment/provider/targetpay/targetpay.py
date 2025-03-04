# vim: set ts=8 sw=4 sts=4 et ai:
import json
from socket import error, timeout
from urllib import urlencode
from urllib2 import URLError

from osso.core.http.shortcuts import HTTPError, Options, http_get, opt_secure
from osso.payment import (
    BuyerError, PaymentAlreadyUsed, PaymentSuspect,
    ProviderError, ProviderBadConfig, ProviderDown)
from osso.payment.base import Provider
from osso.payment.conditional import log, reverse, settings
from osso.payment.models import AtomicUpdateFailed, Payment
from osso.payment.signals import payment_updated


http_opt = (opt_secure | Options(timeout=10))


class AtomicUpdateDupe(AtomicUpdateFailed):
    pass


class TargetpayBase(object):
    provider_url = 'https://www.targetpay.com'

    def __init__(self, testing=False, rtlo=None):
        self.rtlo = (
            rtlo or
            getattr(settings, 'OSSO_PAYMENT_TARGETPAY', {}).get('rtlo'))
        self.test_mode = testing

    def get_start_url(self):
        return '{}/{}/start'.format(self.provider_url, self.provider_sub)

    def get_start_parameters(self):
        parameters = {
            'rtlo': self.rtlo,
            'description': self.payment.description,
            'amount': int(self.payment.amount * 100),
            'returnurl': self.build_absolute_uri(reverse(
                'osso_payment_targetpay_return',
                kwargs={'payment_id': self.payment.id})),
            'cancelurl': self.build_absolute_uri(reverse(
                'osso_payment_targetpay_abort',
                kwargs={'payment_id': self.payment.id})),
            'reporturl': self.build_absolute_uri(reverse(
                'osso_payment_targetpay_report',
                kwargs={'payment_id': self.payment.id})),
        }
        return parameters

    def get_check_url(self):
        return '{}/{}/check'.format(self.provider_url, self.provider_sub)

    def get_check_parameters(self, payment):
        parameters = {
            'rtlo': self.rtlo,
            'trxid': payment.unique_key.split('-', 1)[1],
            # > Als u voor once '1' invult dan zal slechts 1x een OK status
            # > teruggegeven worden.  Als de bovenstaande URL nog een keer
            # > wordt aangeroepen voor hetzelfde Transactie ID dan krijgt u
            # > een foutmelding 'TP00014 (Reeds ingewisseld)' terug.  Als u
            # > voor once '0' invult dan zal steeds een OK status terug
            # > blijven komen.
            'once': '0',  # no need, we use atomic db update
        }
        return parameters

    def get_payment_form(self, payment):
        """
        Return the verbatim HTML form that should be (auto)submitted by
        the user to go to the payment page.

        Use once only (because we set properties on this object).
        (A bit of a hack; it'd be better it we abstracted that away to
        another instance, but not right now.)
        """
        # Set payment locally for later use; less argument passing.
        assert not hasattr(self, 'payment'), self.payment
        self.payment = payment

        host_prefix = payment.realm
        if '://' not in host_prefix:
            host_prefix = 'http://%s' % (host_prefix,)
        self.build_absolute_uri = (lambda x: host_prefix + x)

        if payment.transfer_initiated:
            raise PaymentAlreadyUsed()  # user clicked back?

        payment_url = self.start_transaction()
        return self.create_html_form_from_url(payment_url, 'targetpay_form')

    def parse_status(self, text):
        # 000000 OK
        # TP0012 Transaction has expired
        # .. or, also ..
        # 000000 177XXX584|https://www.targetpay.com/SUB/launch?...

        if ' ' in text:
            status, rest = text.split(' ', 1)
        else:
            assert status != '000000', text
            rest = ''
        return status, rest

    def start_transaction(self):
        parameters = self.get_start_parameters()

        # https://www.targetpay.com/SUB/start?rtlo=$NUMBER&
        #   description=test123&amount=123&
        #   returnurl=https://$HOST/return&
        #   cancelurl=https://$HOST/cancel&
        #   reporturl=https://$HOST/report&test=1&ver=3
        ret = self._do_request(self.get_start_url(), parameters)
        # 000000 177XXX584|https://www.targetpay.com/SUB/launch?
        #   trxid=177XXX584&ec=779XXXXXXXXX273
        status_code, status_text = self.parse_status(ret)

        if status_code == '000000':
            pass
        elif status_code == '000001' and self.test_mode:
            pass
        else:
            self.handle_status_error(self.payment, ret)
            assert False  # should not get here

        try:
            trxid, payment_url = status_text.split('|', 1)
            if not payment_url.startswith('https:'):
                raise ValueError('no https?')
        except ValueError:
            self.handle_status_error(self.payment, ret)
            assert False  # should not get here

        # Initiate it and store the unique_key with our submethod as
        # first argument. (Does an atomic check.)
        self.payment.set_unique_key('{}-{}'.format(self.provider_sub, trxid))

        return payment_url

    def request_status(self, payment, request):
        """
        Check status at payment backend and store value locally.
        """
        parameters = self.get_check_parameters(payment)
        ret = self._do_request(self.get_check_url(), parameters)

        # 000000 OK
        # TP0012 Transaction has expired
        # etc..
        status_code, status_text = self.parse_status(ret)
        self.handle_status(
            payment, status_code, status_text, request_data=request.POST)

    def handle_status(self, payment, status_code, status_text, request_data):
        if status_code == '000000':
            self.handle_status_paid(
                payment, status_code, status_text, request_data)

        elif status_code == 'TP0010':  # Transaction has not been completed
            assert payment.state == 'submitted', (payment.pk, payment.state)

        elif status_code in ('TP0011', 'TP0012', 'TP0013'):
            # TP0011: creditcard: Transaction has been cancelled
            # TP0011: ideal: Transaction has been cancelled
            # TP0011: mrcash: Transaction has failed
            # TP0012: ideal: Transaction has expired
            # TP0012: mrcash: Transaction not finished and expired
            # TP0013: creditcard: Transaction was cancelled
            # TP0013: mrcash: Transaction has been cancelled (by user)
            self.handle_status_aborted(payment, status_code, status_text)

        elif status_code == 'TP0014':  # Already used
            assert payment.state == 'final'

        else:
            self.handle_status_error(
                payment, '{} {}'.format(status_code, status_text))
            assert False  # should not get here

    def handle_status_paid(self, payment, status_code, status_text,
                           request_data):
        try:
            payment.mark_passed()
            payment.mark_succeeded()
        except AtomicUpdateFailed:
            if Payment.objects.get(pk=payment.pk).is_success is True:
                raise AtomicUpdateDupe(status_code, status_text)
            raise
        else:
            payment_updated.send(sender=payment, change='passed')
            payment.set_blob(
                'targetpay.{provider_sub}: {json_blob}'.format(
                    provider_sub=self.provider_sub,
                    json_blob=json.dumps(request_data)))

    def handle_status_aborted(self, payment, status_code, status_text):
        try:
            payment.mark_aborted()
        except AtomicUpdateFailed:
            if Payment.objects.get(pk=payment.pk).is_success is False:
                raise AtomicUpdateDupe(status_code, status_text)
            raise
        else:
            payment_updated.send(sender=payment, change='aborted')

    def handle_status_error(self, payment, response):
        # FIXME: For error that we didn't handle, we should raise one
        # of the exceptions below:
        if False:
            raise BuyerError()
            raise PaymentSuspect()
            raise ProviderBadConfig()
            raise ProviderError()
            raise ProviderDown()
        raise ValueError('payment: %d\nresponse: %r' % (
            payment.id, response))

    def _do_request(self, url, parameters):
        url = '{url}?{parameters}'.format(
            url=url, parameters=urlencode(parameters))

        for attempt in range(3):
            log(url, 'targetpay', 'qry.{}'.format(self.provider_sub))
            try:
                ret = http_get(url, opt=http_opt)
            except (HTTPError, URLError, error, timeout) as e:
                log('connection error #{}: {}'.format(attempt + 1, e),
                    'targetpay', 'ret.{}'.format(self.provider_sub))
            else:
                break
        else:
            raise
        log(ret, 'targetpay', 'ret.{}'.format(self.provider_sub))

        return ret


class TargetpayCreditcard(TargetpayBase, Provider):
    provider_sub = 'creditcard'
    provider_sub_url = 'creditcard_atos'  # or 'creditcard' for old style

    def get_start_url(self):
        return '{}/{}/start'.format(self.provider_url, self.provider_sub_url)

    def get_start_parameters(self):
        parameters = super(TargetpayCreditcard, self).get_start_parameters()
        del parameters['cancelurl']

        # Unused, for old creditcard method.
        if self.provider_sub_url == 'creditcard':
            parameters['currency'] = 'EUR'

        if self.test_mode:
            # > Wanneer deze parameter meegegeven wordt als "1", vindt
            # > de bepaling plaats op het pre-productie platform.
            # > U kunt rtlo code 41980 gebruiken om te testen.
            # > Er kan een test kaartnummer gebruikt worden (Visa 4236
            # > 8615 8842 3130, vervaldatum in de toekomst, CVV 123).
            # OK status will not be "000000" but "000001".
            parameters['test'] = '1'
            parameters['rtlo'] = '41980'

        return parameters

    def get_check_url(self):
        return '{}/{}/check'.format(self.provider_url, self.provider_sub_url)

    def get_check_parameters(self, payment):
        parameters = super(TargetpayCreditcard, self).get_check_parameters(
            payment)

        if self.test_mode:
            # > Vul hier 1 in en de transactie wordt ook als OK
            # > aangemerkt als deze nog niet betaald is. Alle andere
            # > checks worden wel net als normaal doorlopen.
            # Is still needed if you didn't replace the rtlo in the
            # get_start_parameters test mode.
            # #parameters['test'] = '1'
            parameters['rtlo'] = '41980'

        return parameters

    def handle_status(
            self, payment, status_code, status_text, request_data):
        """
        The Transaction failed status apparently is non-final for
        creditcard payments. And so is the Cancelled status.

        Occurrence was last seen on 2017-09-04 after which this
        "fix"/workaround was re-enabled.
        """
        if status_code == 'TP0011':
            # TP0011: creditcard: Transaction failed
            #
            # However, this can apparently be reopened at any time,
            # because it has happened that this was followed up by
            # Success state.
            #
            # Third example on 2017-07-05:
            # 17:45:45+0200: report: <QueryDict: {u'status': [u'Failed'], ..}>
            # 17:45:45+0200: qry.creditcard: ..com/creditcard_atos/check?...
            # 17:45:45+0200: ret.creditcard: TP0011 Transaction failed
            # 17:47:05+0200: report: <QueryDict: {u'status': [u'Success'], ..}>
            # 17:47:05+0200: qry.creditcard: ..com/creditcard_atos/check?...
            # 17:47:05+0200: ret.creditcard: 000000 OK
            #
            # Do not mark_aborted() because we cannot accept success
            # later on.
            assert payment.state == 'submitted', (payment.pk, payment.state)

        elif status_code == 'TP0013':
            # TP0013: creditcard: Transaction was cancelled
            #
            # However, this can apparently be reopened at any time,
            # because it has happened that this was followed up by
            # Success state.
            #
            # First example on 2017-06-29:
            # 22:16:03+0200: report: <QueryDict: {u'status': [u'Cancelled']..
            # 22:16:03+0200: qry.creditcard: ..com/creditcard_atos/check?...
            # 22:16:03+0200: ret.creditcard: TP0013 Transaction was cancelled
            # 22:16:49+0200: report: <QueryDict: {u'status': [u'Success']..
            # 22:16:49+0200: qry.creditcard: ..com/creditcard_atos/check?...
            # 22:16:49+0200: ret.creditcard: 000000 OK
            #
            # Most recent example on 2018-06-29.
            #
            # Do not mark_aborted() because we cannot accept success
            # later on.
            assert payment.state == 'submitted', (payment.pk, payment.state)

        else:
            super(TargetpayCreditcard, self).handle_status(
                payment, status_code, status_text, request_data)


class TargetpayIdeal(TargetpayBase, Provider):
    # This should be an IdealProvider, not a Provider, but that's only
    # true once we implement get_banks(). However, things works fine by
    # letting Targetpay show the bank selection instead.
    provider_sub = 'ideal'

    def get_start_parameters(self):
        parameters = super(TargetpayIdeal, self).get_start_parameters()
        parameters['ver'] = '3'

        if self.test_mode:
            # > Om uw orderafhandeling te testen kunt u bij de start
            # > functie uit paragraaf 3 de parameter
            # > test=1 opgeven. Met deze instelling krijgt u altijd een
            # > '00000 OK' status terug wanneer u de check functie uit
            # > paragraaf 5 aanroept.
            # You'll return to the returnurl.
            parameters['test'] = '1'

        return parameters


class TargetpayMrCash(TargetpayBase, Provider):
    provider_sub = 'mrcash'
    language_codes = ('NL', 'FR', 'EN')  # limited valid languages

    def get_start_parameters(self):
        parameters = super(TargetpayMrCash, self).get_start_parameters()

        assert self.language_code in self.language_codes, self.language_code
        parameters['lang'] = self.language_code
        assert self.remote_addr, 'remote_addr is mandatory!'
        parameters['userip'] = self.remote_addr

        return parameters

    def get_check_parameters(self, payment):
        parameters = super(TargetpayMrCash, self).get_check_parameters(payment)

        if self.test_mode:
            # > Vul hier 1 in en de transactie wordt ook als OK
            # > aangemerkt als deze nog niet betaald is. Alle andere
            # > checks worden wel net als normaal doorlopen.
            parameters['test'] = '1'

        return parameters

    def get_payment_form(self, payment, locale=None, remote_addr=None):
        """
        Return the verbatim HTML form that should be (auto)submitted by
        the user to go to the payment page.

        Use once only (because we set properties on this object).
        (A bit of a hack; it'd be better it we abstracted that away to
        another instance, but not right now.)
        """
        locale = locale or 'nl_NL'
        assert len(locale.split('_')) == 2, locale  # looks like nl_NL ?
        self.language_code = locale.split('_')[0].upper()  # NL/FR/EN
        self.remote_addr = remote_addr or ''

        return super(TargetpayMrCash, self).get_payment_form(payment)
