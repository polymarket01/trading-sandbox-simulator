"""Explicit, one-time instance provisioning. Never trims or imports an existing trading database."""
from copy import deepcopy
from decimal import Decimal
import json,os
from pathlib import Path
from sqlalchemy import select,func
from app.core.config import settings
from app.core.security import generate_api_key,generate_api_secret,hash_password
from app.models.market import Market
from app.models.user import User
from app.models.market_strategy_config import MarketStrategyConfig
from app.models.paper_exchange import PaperAsset,PaperBrandConfig,PaperSystemSetting
from app.models.contract_risk_limit_tier import ContractRiskLimitTier
from app.services.paper_account_service import ensure_global_run,ensure_user_assets
from app.services.strategy_config_service import ensure_strategy_templates,set_selected_market_strategy
from app.services.strategy_accounts import allocate_accounts
from app.services.maker_plugins import get_plugin,internal_default,internal_validate


def load_profile(path):
    profile=json.loads(Path(path).read_text())
    if profile.get('schema_version')!=1 or not profile.get('profile_id'):
        raise ValueError('Invalid instance profile version/id')
    symbols=[item['market']['symbol'] for item in profile['markets']]
    if not symbols or len(set(symbols))!=len(symbols):raise ValueError('Duplicate or empty markets')
    for item in profile['markets']:
        m=item['market'];key=item['strategy_key']
        if key!='NONE' and m['product_type'] not in get_plugin(key).manifest['products']:
            raise ValueError('Unsupported strategy/product')
        if Decimal(str(m['price_tick']))<=0 or Decimal(str(m['qty_step']))<=0:
            raise ValueError('Invalid tick/step')
    return profile


def _ensure_flow_file(profile):
    path=Path(settings.sandbox_data_dir)/'flow_config.json'
    if path.exists():return
    from app.services.independent_flow_service import FlowConfig
    docs={i['market']['symbol']:{'version':1,'config':FlowConfig.model_validate(i['flow']).model_dump(mode='json')} for i in profile['markets']}
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(docs,ensure_ascii=False,indent=2))
    temporary.chmod(0o600);temporary.replace(path)


async def bootstrap_instance_profile(session,runtime):
    profile=load_profile(settings.instance_profile_path)
    marker=await session.scalar(select(PaperSystemSetting).where(PaperSystemSetting.key=='instance_profile'))
    if marker is not None:
        if marker.value_json.get('profile_id')!=profile['profile_id']:
            raise ValueError('Profile mismatch: explicit migration or a fresh data directory required')
        runtime.paper_global_run_id=(await ensure_global_run(session)).run_id
        _ensure_flow_file(profile)
        return
    if await session.scalar(select(func.count()).select_from(User)) or await session.scalar(select(func.count()).select_from(Market)):
        raise ValueError('Instance profile requires a fresh database; existing users/markets/history are not deleted automatically')
    decimal_fields={column.name for column in Market.__table__.columns if str(column.type).startswith('NUMERIC')}
    markets=[]
    for item in profile['markets']:
        values=deepcopy(item['market'])
        for key in decimal_fields & values.keys():values[key]=Decimal(str(values[key]))
        if values['price_source']=='manual':
            values.update(index_price_source='manual',funding_rate_mode='fixed',funding_rate=Decimal(0))
        market=Market(**values,is_active=True,paper_status='TRADING',visibility='listed')
        session.add(market);await session.flush();markets.append(market)
        if market.product_type=='PERP':
            session.add(ContractRiskLimitTier(market_id=market.id,tier=1,notional_floor=0,max_leverage=market.max_leverage,maintenance_margin_rate=market.maintenance_margin_rate,maintenance_amount=0))
    for code in sorted({'USDT',*(m.base_asset for m in markets)}):
        session.add(PaperAsset(code=code,display_name=code,description='模拟资产',display_precision=8,status='ACTIVE'))
    session.add(PaperBrandConfig(id=1,exchange_name='Sandbox Exchange',primary_color='#22d3ee',default_language='zh-CN',footer_text='模拟交易',paper_notice='所有资产与交易均为沙盒模拟。'))
    runtime.paper_global_run_id=(await ensure_global_run(session)).run_id
    for key,value in {'paper_market_symbols':[m.symbol for m in markets],'paper_global_run_id':runtime.paper_global_run_id,'paper_defaults':{'spot_initial_usdt':'100000000','perp_initial_usdt':'100000000','default_leverage':'5','user_reset_enabled':True}}.items():
        session.add(PaperSystemSetting(key=key,value_json=value))
    await session.flush()
    for uid,name,role,password in [(1,'admin','admin',os.environ['PAPER_EXCHANGE_ADMIN_PASSWORD']),(1000,'trader','user',os.environ['SANDBOX_TRADER_PASSWORD'])]:
        user=User(id=uid,username=name,username_normalized=name,role=role,is_active=True,password_hash=hash_password(password),api_key=os.environ.get('PAPER_EXCHANGE_ADMIN_API_KEY') if role=='admin' else generate_api_key('trader'),api_secret_hash=generate_api_secret(),account_epoch=0)
        if not user.api_key:user.api_key=generate_api_key('admin')
        session.add(user);await session.flush()
        await ensure_user_assets(session,user,markets,reason='instance_initialization')
    await ensure_strategy_templates(session)
    from app.services.contract_ladder_service import market_metadata,CAPABILITIES
    for market,item in zip(markets,profile['markets']):
        key=item['strategy_key']
        if key=='NONE':continue
        uids=await allocate_accounts(session,market,'maker',key)
        config=deepcopy(item.get('strategy_config')) or internal_default(key,market.price_source_symbol)
        config['enabled']=True
        config=internal_validate(key,config,market_metadata(market,uids[0]),CAPABILITIES)
        await set_selected_market_strategy(session,market,key,config_json={'desired_version':1,'config':config},updated_by_user_id=1)
    session.add(PaperSystemSetting(key='instance_profile',value_json={'profile_id':profile['profile_id'],'schema_version':1}))
    await session.commit()
    _ensure_flow_file(profile)
