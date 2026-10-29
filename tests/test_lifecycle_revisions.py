from dataclasses import replace
from pathlib import Path
import json

import pandas as pd
import pytest

from etf_flow_monitor.config import FlowMonitorConfig
from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.lifecycle import (
    apply_manual_confirmations, extract_lifecycle_events_from_announcements,
    requires_lifecycle_review,
    apply_flow_adjustments_to_snapshot, match_share_jumps_to_events,
)
from tools import build_etf_lifecycle_table as lifecycle


def test_confirmation_invalidated_by_revised_values():
    audit = pd.DataFrame([dict(fund_code='159528.SZ', trade_date='20261020',
        prev_trade_date='20261019', share_change=-600., share_change_pct=-0.45,
        match_status='unmatched')])
    old = audit.assign(share_change=600., share_change_pct=0.82)
    assert apply_manual_confirmations(audit, old).iloc[0].match_status == 'unmatched'
    confirmed = apply_manual_confirmations(audit, audit.assign(share_change=-600.00000000001))
    assert confirmed.iloc[0].match_status == 'manual_confirmed_no_lifecycle_event'


def test_meeting_proposal_is_neither_effective_event_nor_automatic_no_event():
    title = '关于以通讯方式召开基金份额持有人大会的公告'
    content = '审议终止上市议案；若表决通过，2026年11月20日进入清算。'
    assert requires_lifecycle_review(title)
    ann = pd.DataFrame([dict(fund_code='159528.SZ', announcement_date='20261017',
                            title=title, content=content)])
    assert extract_lifecycle_events_from_announcements(ann).empty
    plan = pd.DataFrame([dict(fund_code='159528.SZ', trade_date='20261019',
        prev_trade_date='20261016', request_start_date='20261012', request_end_date='20261026',
        share_change=600., share_change_pct=0.82)])
    assert lifecycle._auto_confirm_non_lifecycle_announcement_windows(plan, ann).empty
    warning=ann.assign(title='关于可能触发基金合同终止情形的风险提示公告',
                       content='若触发条件，基金将进入清算程序。')
    assert extract_lifecycle_events_from_announcements(warning).empty
    ambiguous=warning.assign(title='关于某基金的提示性公告',
                              content='本基金可能触发基金合同终止情形，届时进入清算。')
    assert extract_lifecycle_events_from_announcements(ambiguous).empty


def test_content_fingerprint_rejects_same_date_revisions_and_old_status(tmp_path):
    config = replace(FlowMonitorConfig(), cache_dir=tmp_path/'cache')
    share = config.cache_dir/'tushare/daily_cross_section/etf_share/20261020.csv'
    share.parent.mkdir(parents=True)
    share.write_text('fund_code,shares\n159528.SZ,600\n', encoding='utf-8')
    observation = tmp_path/'observation.csv'
    observation.write_text('fund_code\n159528.SZ\n', encoding='utf-8')
    refs = {'observations': observation}
    fingerprint = lambda: lifecycle._audit_fingerprint(config, refs, {'threshold': 0.5})
    original = fingerprint()
    status = tmp_path/'status.json'
    bounds = dict(data_earliest_date=pd.Timestamp('20261020'), data_latest_date=pd.Timestamp('20261020'))
    lifecycle._update_lifecycle_status(status, **bounds, local_cache_start_date=None,
        lifecycle_current=True, request_plan_rows=0, input_fingerprint=original)
    assert lifecycle._status_is_current(status, **bounds, input_fingerprint=original)
    assert not lifecycle._status_is_current(status, **bounds)
    share.write_text('fund_code,shares\n159528.SZ,700\n', encoding='utf-8')
    assert not lifecycle._status_is_current(status, **bounds, input_fingerprint=fingerprint())
    lifecycle._update_lifecycle_status(status, **bounds, local_cache_start_date=None,
        lifecycle_current=False, request_plan_rows=1, input_fingerprint=fingerprint())
    assert json.loads(status.read_text())['verified_through'] == ''
    revised = fingerprint()
    observation.write_text('fund_code\n', encoding='utf-8')
    assert fingerprint() != revised


def test_skip_preserves_observation_and_all_reference_bytes(tmp_path, monkeypatch):
    config = FlowMonitorConfig()
    config = replace(config, **{k: tmp_path/getattr(config,k) for k in config.__dataclass_fields__
                              if isinstance(getattr(config,k), Path)})
    config_path = tmp_path/'config.txt'
    config_path.write_text('\n'.join(f'{k} = {getattr(config,k)}' for k in config.__dataclass_fields__),encoding='utf-8')
    cache = CacheStore(config.cache_dir)
    cache.save_static_frame('tushare','etf_basic','E',pd.DataFrame([{'fund_code':'510300.SH'}]))
    for day, value in [('20261019',100),('20261020',100)]:
        cache.save_daily_cross_section('tushare','etf_share',day,
            pd.DataFrame([{'fund_code':'510300.SH','trade_date':day,'shares':value}]))
    # No jumps means no calendar request. A first run creates a complete status.
    assert lifecycle.main(['--config',str(config_path),'--force']) == 0
    config.lifecycle_observation_plan_path.write_text('fund_code,trade_date\n510300.SH,20261020\n',encoding='utf-8')
    # Capture the fingerprint used by an actual audit invocation, then mark the
    # constructed observation as part of its saved complete state.
    original = lifecycle._audit_fingerprint
    captured=[]
    def fingerprint(*args,**kwargs):
        value=original(*args,**kwargs);captured.append((args,kwargs));return value
    monkeypatch.setattr(lifecycle,'_audit_fingerprint',fingerprint)
    # Deliberately stale status must trigger an audit instead of skipping.
    assert lifecycle.main(['--config',str(config_path),'--skip-if-current']) == 0
    config.lifecycle_observation_plan_path.write_text('fund_code,trade_date\n510300.SH,20261020\n',encoding='utf-8')
    args,kwargs=captured[-1]
    status=json.loads(config.lifecycle_status_path.read_text(encoding='utf-8-sig'))
    status['input_fingerprint']=original(*args,**kwargs)
    config.lifecycle_status_path.write_text(json.dumps(status),encoding='utf-8')
    tracked=[config.lifecycle_observation_plan_path,config.lifecycle_request_plan_path,
             config.lifecycle_flow_adjustments_path,config.lifecycle_status_path]
    before={p:p.read_bytes() for p in tracked}
    assert lifecycle.main(['--config',str(config_path),'--skip-if-current']) == 0
    assert before == {p:p.read_bytes() for p in tracked}


def test_verified_split_date_does_not_zero_nearby_ordinary_jump():
    ann=pd.DataFrame([dict(fund_code='159994.SZ',announcement_date='20260209',
        event_date='20260206',title='关于基金份额拆分结果的公告')])
    events=extract_lifecycle_events_from_announcements(ann)
    jumps=pd.DataFrame([dict(fund_code='159994.SZ',trade_date=pd.Timestamp(day))
                        for day in ['20260206','20260209']])
    audit=match_share_jumps_to_events(jumps,events)
    assert audit.match_status.tolist()==['matched_lifecycle_event','unmatched']


def test_report_rejects_stale_adjustment_after_share_revision():
    flow=pd.DataFrame([dict(fund_code='159994.SZ',trade_date='20260209',
        share_change=100.,estimated_net_flow=1e6)])
    adjustment=pd.DataFrame([dict(fund_code='159994.SZ',trade_date='20260209',
        share_change=10000.,adjustment_action='zero_estimated_net_flow')])
    with pytest.raises(ValueError,match='Stale lifecycle adjustments'):
        apply_flow_adjustments_to_snapshot(flow,adjustment)
    first_quote=flow.assign(share_change=float('nan'),estimated_net_flow=0.)
    assert apply_flow_adjustments_to_snapshot(first_quote,adjustment).estimated_net_flow.eq(0).all()


@pytest.mark.parametrize('source',['cninfo','sse','szse'])
def test_error_response_cannot_be_treated_as_empty_announcement_window(monkeypatch,source):
    from etf_flow_monitor.data.cninfo_announcements import CninfoAnnouncementClient
    from etf_flow_monitor.data.exchange_announcements import ExchangeAnnouncementClient
    cls=CninfoAnnouncementClient if source=='cninfo' else ExchangeAnnouncementClient
    monkeypatch.setattr(cls,'_read_text',lambda *a:'{"error":"rate limited"}')
    if source=='cninfo':
        monkeypatch.setattr(cls,'_stock_param',lambda *a:'159528')
    code='510300.SH' if source=='sse' else '159528.SZ'
    with pytest.raises(RuntimeError,match='cannot confirm an empty window'):
        cls().fetch(code,start_date='20261001',end_date='20261020')


def test_remote_announcement_response_saved_before_parsing(tmp_path,monkeypatch):
    import io
    import urllib.request
    from unittest.mock import Mock
    from etf_flow_monitor.data.cninfo_announcements import CninfoAnnouncementClient
    response=io.BytesIO(b'{"announcements":[],"totalRecordNum":0}')
    response.headers=Mock()
    response.headers.get_content_charset.return_value='utf-8'
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:response)
    client=CninfoAnnouncementClient(ignore_proxy=False,cache_dir=tmp_path)
    text=client._read_text(urllib.request.Request('https://www.cninfo.com.cn/new/hisAnnouncement/query'))
    cached=list((tmp_path/'cninfo/raw/announcements').rglob('*.json'))
    assert len(cached)==1
    assert json.loads(cached[0].read_text(encoding='utf-8-sig'))['rows'][0]['response_text']==text
