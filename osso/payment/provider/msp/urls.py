# vim: set ts=8 sw=4 sts=4 et ai:
from osso.payment.conditional import patterns, url

from .views import TransactionAbort, TransactionReport, TransactionReturn


# We expect this to be included as ^api/msp/
urlpatterns = patterns('',  # noqa
    # URL: http://SOMEWHERE/api/msp/PAYMENTID/return/
    # Here we have to call the msp API and check that the payment
    # succeeded.
    url(r'^(?P<payment_id>[0-9A-Fa-f]+)/return/$',
        TransactionReturn.as_view(),
        name='osso_payment_msp_return'),

    # URL: http://SOMEWHERE/api/msp/PAYMENTID/abort/
    # Abort/cancel the transaction.
    url(r'^(?P<payment_id>[0-9A-Fa-f]+)/abort/$',
        TransactionAbort.as_view(),
        name='osso_payment_msp_abort'),

    # URL: http://SOMEWHERE/api/msp/report/?transactionid=1234
    # You need to put the URL in the MSP merchant configuration as well.
    url(r'^report/$',
        TransactionReport.as_view(),
        name='osso_payment_msp_report'),
)
