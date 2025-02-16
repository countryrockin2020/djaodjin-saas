# Copyright (c) 2021, DjaoDjin inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
from __future__ import unicode_literals

from collections import OrderedDict

from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView, CreateAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (CreateOfflineTransactionSerializer,
    OfflineTransactionSerializer, TransactionSerializer)
from ..decorators import _valid_manager
from ..docs import swagger_auto_schema, OpenAPIResponse
from ..filters import DateRangeFilter, OrderingFilter, SearchFilter
from ..mixins import OrganizationMixin, ProviderMixin, DateRangeContextMixin
from ..models import (get_broker, sum_orig_amount, Subscription, Transaction,
    Organization, Plan)
from ..backends import ProcessorError
from ..pagination import BalancePagination


class StatementBalancePagination(PageNumberPagination):
    """
    Decorate the results of an API call with the balance as shown
    in an organization statement.
    """

    def paginate_queryset(self, queryset, request, view=None):
        self.start_at = view.start_at
        self.ends_at = view.ends_at
        self.balance_amount, self.balance_unit \
            = Transaction.objects.get_statement_balance(view.organization)
        return super(StatementBalancePagination, self).paginate_queryset(
            queryset, request, view=view)

    def get_paginated_response(self, data):
        return Response(OrderedDict([
            ('start_at', self.start_at),
            ('ends_at', self.ends_at),
            ('balance_amount', self.balance_amount),
            ('balance_unit', self.balance_unit),
            ('count', self.page.paginator.count),
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('results', data)
        ]))


class TotalPagination(PageNumberPagination):

    def paginate_queryset(self, queryset, request, view=None):
        self.start_at = view.start_at
        self.ends_at = view.ends_at
        self.totals = view.totals
        return super(TotalPagination, self).paginate_queryset(
            queryset, request, view=view)

    def get_paginated_response(self, data):
        return Response(OrderedDict([
            ('start_at', self.start_at),
            ('ends_at', self.ends_at),
            ('total', self.totals['amount']),
            ('unit', self.totals['unit']),
            ('count', self.page.paginator.count),
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('results', data)
        ]))


class IncludesSyncErrorPagination(PageNumberPagination):

    def paginate_queryset(self, queryset, request, view=None):
        if view and hasattr(view, 'processor_error'):
            self.detail = view.processor_error
        return super(IncludesSyncErrorPagination, self).paginate_queryset(
            queryset, request, view=view)

    def get_paginated_response(self, data):
        paginated = [
            ('count', self.page.paginator.count),
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('results', data)
        ]
        if hasattr(self, 'detail'):
            paginated += [('detail', self.detail)]
        return Response(OrderedDict(paginated))


class TotalAnnotateMixin(object):

    def get_queryset(self):
        queryset = super(TotalAnnotateMixin, self).get_queryset()
        balances = sum_orig_amount(queryset)
        if len(balances) > 1:
            raise ValueError(_("balances with multiple currency units (%s)") %
                str(balances))
        # `sum_orig_amount` guarentees at least one result.
        self.totals = balances[0]
        return queryset


class TransactionFilterMixin(DateRangeContextMixin):
    """
    ``Transaction`` list result of a search query, filtered by dates.
    """

    search_fields = ('descr',
                     'orig_organization__full_name',
                     'dest_organization__full_name')

    filter_backends = (DateRangeFilter, SearchFilter)


class SmartTransactionListMixin(TransactionFilterMixin):
    """
    ``Transaction`` list which is also searchable and sortable.
    """
    ordering_fields = [('descr', 'description'),
                           ('dest_amount', 'amount'),
                           ('dest_organization__slug', 'dest_organization'),
                           ('dest_account', 'dest_account'),
                           ('orig_organization__slug', 'orig_organization'),
                           ('orig_account', 'orig_account'),
                           ('created_at', 'created_at')]

    filter_backends = (TransactionFilterMixin.filter_backends +
        (OrderingFilter,))


class TransactionQuerysetMixin(object):

    def get_queryset(self):
        self.selector = self.request.GET.get('selector', None)
        if self.selector is not None:
            return Transaction.objects.filter(
                Q(dest_account__icontains=self.selector)
                | Q(orig_account__icontains=self.selector))
        return Transaction.objects.all()


class TransactionListAPIView(SmartTransactionListMixin,
                             TransactionQuerysetMixin, ListAPIView):
    """
    Lists ledger transactions

    Queries a page (``PAGE_SIZE`` records) of ``Transaction`` from
    the :doc:`ledger <ledger>`.

    The queryset can be filtered to a range of dates
    ([``start_at``, ``ends_at``]) and for at least one field to match a search
    term (``q``).

    Query results can be ordered by natural fields (``o``) in either ascending
    or descending order (``ot``).

    **Tags**: billing

    **Examples**

    .. code-block:: http

        GET /api/billing/transactions?start_at=2015-07-05T07:00:00.000Z\
&o=date&ot=desc HTTP/1.1

    responds

    .. code-block:: json

        {
            "ends_at": "2017-03-30T18:10:12.962859Z",
            "balance": 11000,
            "unit": "usd",
            "count": 1,
            "next": null,
            "previous": null,
            "results": [
                {
                    "created_at": "2017-02-01T00:00:00Z",
                    "description": "Charge for 4 periods",
                    "amount": "($356.00)",
                    "is_debit": true,
                    "orig_account": "Liability",
                    "orig_organization": "xia",
                    "orig_amount": 112120,
                    "orig_unit": "usd",
                    "dest_account": "Funds",
                    "dest_organization": "stripe",
                    "dest_amount": 112120,
                    "dest_unit": "usd"
                }
            ]
        }
    """
    pagination_class = BalancePagination
    serializer_class = TransactionSerializer


class BillingsQuerysetMixin(OrganizationMixin):

    def get_queryset(self):
        """
        Get the list of transactions for this organization.
        """
        return Transaction.objects.by_customer(self.organization)


class BillingsAPIView(SmartTransactionListMixin,
                      BillingsQuerysetMixin, ListAPIView):
    """
    Lists subscriber transactions

    Queries a page (``PAGE_SIZE`` records) of ``Transaction`` associated
    to ``{organization}`` while the organization acts as a subscriber.

    The queryset can be filtered to a range of dates
    ([``start_at``, ``ends_at``]) and for at least one field to match a search
    term (``q``).

    Query results can be ordered by natural fields (``o``) in either ascending
    or descending order (``ot``).

    This API end point is typically used to display orders, payments and refunds
    of a subscriber (see :ref:`subscribers pages <pages_subscribers>`)

    **Tags**: billing

    **Examples**

    .. code-block:: http

         GET /api/billing/xia/history?start_at=2015-07-05T07:00:00.000Z\
&o=date&ot=desc HTTP/1.1

    responds

    .. code-block:: json

        {
            "count": 1,
            "next": null,
            "previous": null,
            "balance": 11000,
            "unit": "usd",
            "results": [
                {
                    "created_at": "2015-08-01T00:00:00Z",
                    "description": "Charge for 4 periods",
                    "amount": "($356.00)",
                    "is_debit": true,
                    "orig_account": "Liability",
                    "orig_organization": "xia",
                    "orig_amount": 112120,
                    "orig_unit": "usd",
                    "dest_account": "Funds",
                    "dest_organization": "stripe",
                    "dest_amount": 112120,
                    "dest_unit": "usd"
                }
            ]
        }
    """
    serializer_class = TransactionSerializer
    pagination_class = StatementBalancePagination


class ReceivablesQuerysetMixin(ProviderMixin):

    def get_queryset(self):
        """
        Get the list of transactions for this organization.
        """
        return self.provider.receivables().filter(orig_amount__gt=0)


class ReceivablesListAPIView(TotalAnnotateMixin, TransactionFilterMixin,
                             ReceivablesQuerysetMixin, ListAPIView):
    """
    Lists provider receivables

    Queries a page (``PAGE_SIZE`` records) of ``Transaction`` marked
    as receivables associated to ``{organization}`` while the organization
    acts as a provider.

    The queryset can be filtered to a range of dates
    ([``start_at``, ``ends_at``]) and for at least one field to match a search
    term (``q``).

    Query results can be ordered by natural fields (``o``) in either ascending
    or descending order (``ot``).

    This API endpoint is typically used to find all sales for ``{organization}``
    whether it was paid or not.

    **Tags**: billing

    **Examples**

    .. code-block:: http

         GET /api/billing/cowork/receivables?start_at=2015-07-05T07:00:00.000Z\
&o=date&ot=desc HTTP/1.1

    responds

    .. code-block:: json

        {
            "count": 1,
            "total": "112120",
            "unit": "usd",
            "next": null,
            "previous": null,
            "results": [
                {
                    "created_at": "2015-08-01T00:00:00Z",
                    "description": "Charge <a href='/billing/cowork/receipt/\
1123'>1123</a> distribution for demo562-open-plus",
                    "amount": "112120",
                    "is_debit": false,
                    "orig_account": "Funds",
                    "orig_organization": "stripe",
                    "orig_amount": 112120,
                    "orig_unit": "usd",
                    "dest_account": "Funds",
                    "dest_organization": "cowork",
                    "dest_amount": 112120,
                    "dest_unit": "usd"
                }
            ]
        }
    """
    ordering_fields = [('descr', 'description'),
                           ('dest_amount', 'amount'),
                           ('dest_organization__slug', 'dest_organization'),
                           ('dest_account', 'dest_account'),
                           ('orig_organization__slug', 'orig_organization'),
                           ('orig_account', 'orig_account'),
                           ('created_at', 'created_at')]

    filter_backends = (TransactionFilterMixin.filter_backends +
        (OrderingFilter,))
    serializer_class = TransactionSerializer
    pagination_class = TotalPagination


class TransferQuerysetMixin(ProviderMixin):

    def get_queryset(self):
        """
        Get the list of transactions for this organization.
        """
        try:
            reconcile = not bool(self.request.GET.get('force', False))
            return self.organization.get_transfers(reconcile=reconcile)
        except ProcessorError as err:
            self.processor_error = _("The latest transfers might"\
                " not be shown because there was an error with the backend"\
                " processor (ie. %(err)s).") % {'err': str(err)}
            return Transaction.objects.by_organization(self.organization)


class TransferListAPIView(SmartTransactionListMixin, TransferQuerysetMixin,
                          ListAPIView):
    """
    Lists provider payouts

    Queries a page (``PAGE_SIZE`` records) of ``Transaction`` associated
    to ``{organization}`` while the organization acts as a provider.

    The queryset can be filtered to a range of dates
    ([``start_at``, ``ends_at``]) and for at least one field to match a search
    term (``q``).

    Query results can be ordered by natural fields (``o``) in either ascending
    or descending order (``ot``).

    This API endpoint is typically used to find sales, payments, refunds
    and bank deposits for a provider.
    (see :ref:`provider pages <pages_provider_transactions>`)

    **Tags**: billing

    **Examples**

    .. code-block:: http

         GET /api/billing/cowork/transfers?start_at=2015-07-05T07:00:00.000Z\
&o=date&ot=desc HTTP/1.1

    responds

    .. code-block:: json

        {
            "count": 1,
            "next": null,
            "previous": null,
            "results": [
                {
                    "created_at": "2015-08-01T00:00:00Z",
                    "description": "Charge <a href='/billing/cowork/receipt/\
1123'>1123</a> distribution for demo562-open-plus",
                    "amount": "$1121.20",
                    "is_debit": false,
                    "orig_account": "Funds",
                    "orig_organization": "stripe",
                    "orig_amount": 112120,
                    "orig_unit": "usd",
                    "dest_account": "Funds",
                    "dest_organization": "cowork",
                    "dest_amount": 112120,
                    "dest_unit": "usd"
                }
            ]
        }
    """
    serializer_class = TransactionSerializer
    pagination_class = IncludesSyncErrorPagination


class ImportTransactionsAPIView(ProviderMixin, CreateAPIView):

    serializer_class = CreateOfflineTransactionSerializer

    @swagger_auto_schema(responses={
        201: OpenAPIResponse("", OfflineTransactionSerializer)})
    def post(self, request, *args, **kwargs):
        """
        Inserts an offline transactions.

        The primary purpose of this API call is for a provider to keep
        accurate metrics for the performance of the product sold, regardless
        of payment options (online or offline).

        **Tags**: billing

        **Examples**

        .. code-block:: http

             POST /api/billing/cowork/transfers/import/ HTTP/1.1

        .. code-block:: json

            {
               "created_at": "2020-05-30T00:00:00Z",
               "amount": "10.00",
               "descr": "Paid by check",
               "subscription": "demo562:open-plus"
            }

        responds

        .. code-block:: json

            {
               "detail":"Transaction imported successfully.",
               "results":[
                 {
                   "created_at": "2020-05-30T00:00:00Z",
                   "description": "Paid by check (alice)",
                   "amount": "$10.00",
                   "is_debit": false,
                   "orig_account": "Receivable",
                   "orig_organization": "djaoapp",
                   "orig_amount": 1000,
                   "orig_unit": "usd",
                   "dest_account": "Payable",
                   "dest_organization": "xia",
                   "dest_amount": 1000,
                   "dest_unit": "usd"
                 },
                 {
                   "created_at": "2020-05-30T00:00:00Z",
                   "description": "Paid by check (alice)",
                   "amount": "$10.00",
                   "is_debit": false,
                   "orig_account": "Liability",
                   "orig_organization": "xia",
                   "orig_amount": 1000,
                   "orig_unit": "usd",
                   "dest_account": "Funds",
                   "dest_organization": "djaoapp",
                   "dest_amount": 1000,
                   "dest_unit": "usd"
                 },
                 {
                   "created_at": "2020-05-30T00:00:00Z",
                   "description": "Keep a balanced ledger",
                   "amount": "$10.00",
                   "is_debit": false,
                   "orig_account": "Payable",
                   "orig_organization": "xia",
                   "orig_amount": 1000,
                   "orig_unit": "usd",
                   "dest_account": "Liability",
                   "dest_organization": "xia",
                   "dest_amount": 1000,
                   "dest_unit": "usd"
                 },
                 {
                   "created_at": "2020-05-30T00:00:00Z",
                   "description": "Paid by check (alice)",
                   "amount": "$10.00",
                   "is_debit": false,
                   "orig_account": "Backlog",
                   "orig_organization": "djaoapp",
                   "orig_amount": 1000,
                   "orig_unit": "usd",
                   "dest_account": "Receivable",
                   "dest_organization": "djaoapp",
                   "dest_amount": 1000,
                   "dest_unit": "usd"
                 },
                 {
                   "created_at": "2020-05-30T00:00:00Z",
                 "description":"Paid by check (alice) - Keep a balanced ledger",
                   "amount":"$0.20",
                   "is_debit":false,
                   "orig_account":"Funds",
                   "orig_organization":"djaoapp",
                   "orig_amount":20,
                   "orig_unit":"usd",
                   "dest_account":"Offline",
                   "dest_organization":"djaoapp",
                   "dest_amount":20,
                   "dest_unit":"usd"
                 }
               ]
            }

        """
        return super(ImportTransactionsAPIView, self).post(
            request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        parts = serializer.validated_data['subscription'].split(
            Subscription.SEP)
        if len(parts) != 2:
            raise ValidationError({
                'detail': _("Invalid subscription/plan field format")})
        subscriber = parts[0]
        plan = parts[1]
        subscriber = Organization.objects.filter(slug=subscriber).first()
        if subscriber is None:
            raise ValidationError({'detail': _("Invalid subscriber")})
        plan = Plan.objects.filter(
            slug=plan, organization=self.organization).first()
        if plan is None:
            raise ValidationError({'detail': _("Invalid plan")})
        subscription = Subscription.objects.active_for(
            organization=subscriber).filter(plan=plan).first()
        if subscription is None:
            raise ValidationError({
                'detail': _("Invalid combination of subscriber and plan,"\
" or the subscription is no longer active.")})
        transactions = Transaction.objects.offline_payment(
            subscription, serializer.validated_data['amount'],
            descr=serializer.validated_data['descr'], user=self.request.user,
            created_at=serializer.validated_data.get('created_at'))

        result_data = {
            'detail': _("Transaction imported successfully."),
            'results': TransactionSerializer(
                many=True).to_representation(transactions)
        }
        headers = self.get_success_headers(result_data)
        return Response(result_data,
            status=status.HTTP_201_CREATED, headers=headers)


class StatementBalanceAPIView(OrganizationMixin, APIView):
    """
    Retrieves a customer balance

    Get the statement balance due for an organization.

    **Tags**: billing

    **Examples**

    .. code-block:: http

         GET  /api/billing/cowork/balance/ HTTP/1.1

    responds

    .. code-block:: json

        {
            "balance_amount": "1200",
            "balance_unit": "usd"
        }
    """

    def get(self, request, *args, **kwargs):
        #pylint:disable=unused-argument
        balance_amount, balance_unit \
            = Transaction.objects.get_statement_balance(self.organization)
        return Response({'balance_amount': balance_amount,
                         'balance_unit': balance_unit})

    def delete(self, request, *args, **kwargs):
        """
        Cancels a balance due

        Cancel the balance for a provider organization. This will create
        a transaction for this balance cancellation. A manager can use
        this endpoint to cancel balance dues that is known impossible
        to be recovered (e.g. an external bank or credit card company
        act).

        The endpoint returns the transaction created to cancel the
        balance due.

        **Tags**: billing

        **Examples**

        .. code-block:: http

             DELETE /api/billing/cowork/balance/ HTTP/1.1
        """
        return self.destroy(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs): #pylint:disable=unused-argument
        if not _valid_manager(request, [get_broker()]):
            # XXX temporary workaround to provide GET balance API
            # to subscribers and providers.
            raise PermissionDenied()
        self.organization.create_cancel_transactions(user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
