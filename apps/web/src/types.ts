export type Action='持有'|'加仓'|'减仓'|'退出'|'待买';
export type Holding={code:string;name:string;theme:string;action:Action;weight:number;currentWeight:number;price:number;change:number;thesis:string[];invalidation:string;protectivePrice:number;entry:string;version:string;timestamp:string;risk:string;days:number;kind:'model'|'simulation'};
export type Theme={name:string;phase:string;score:number;breadth:number;flow:number;crowding:number;note:string};
export type Decision={id:string;date:string;title:string;reason:string;version:string;result:string};
export type Snapshot={asOf:string;portfolioStatus?:'healthy'|'partial'|'risk_off'|'observation';portfolioReason?:string;market:{state:string;score:number;exposure:number;style:string;summary:string;factors:{name:string;score:number;note:string}[]};holdings:Holding[];candidates:Holding[];themes:Theme[];decisions:Decision[];equity:number[]};
export type DataProvenance={provider:string;mode:'demo'|'public-prototype'|'research'|'verified';pitVerified:boolean;productionReady:boolean;degradations:string[]};
export type ApiEnvelope<T>={data:T;source:'live'|'demo';stale:boolean;provenance:DataProvenance;message?:string};
