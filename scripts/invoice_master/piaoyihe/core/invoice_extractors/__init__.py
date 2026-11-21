#!/usr/bin/env python3
"""
发票字段提取器模块

该模块提供各种发票类型的字段提取功能，采用策略模式设计，
每种票种有独立的提取器实现，便于维护和扩展。
"""

from .base import InvoiceExtractor
from .common_invoice import CommonInvoiceExtractor
from .factory import InvoiceExtractorFactory
from .fixed_amount_invoice import FixedAmountInvoiceExtractor
from .flight_ticket import FlightTicketExtractor
from .taxi_invoice import TaxiInvoiceExtractor
from .toll_invoice import TollInvoiceExtractor
from .train_ticket import TrainTicketExtractor
from .vehicle_invoice import VehicleInvoiceExtractor

__all__ = [
    'InvoiceExtractor',
    'InvoiceExtractorFactory',
    'CommonInvoiceExtractor',
    'TrainTicketExtractor',
    'FlightTicketExtractor',
    'VehicleInvoiceExtractor',
    'TaxiInvoiceExtractor',
    'FixedAmountInvoiceExtractor',
    'TollInvoiceExtractor',
]
