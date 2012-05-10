import os
import uuid
import json
import pyfaf
import datetime

from django.http import HttpResponse
from django.template import RequestContext
from django.shortcuts import render_to_response
from django.views.decorators.csrf import csrf_exempt

from sqlalchemy import func
from sqlalchemy.sql.expression import desc, literal, distinct

from pyfaf import ureport
from pyfaf.storage.opsys import (OpSys,
                                 OpSysComponent,
                                 Package,
                                 Build)
from pyfaf.storage.report import (Report,
                                  ReportOpSysRelease,
                                  ReportHistoryDaily,
                                  ReportHistoryWeekly,
                                  ReportHistoryMonthly,
                                  ReportPackage,
                                  ReportRelatedPackage)
from pyfaf.hub.reports.forms import (NewReportForm,
                                    ReportFilterForm, ReportOverviewForm)
from pyfaf.hub.common.utils import paginate
from pyfaf.hub.common.queries import ReportHistoryCounts

class AccumulatedHistory(ReportHistoryCounts):
    def __init__(self, db, osrelease_ids, component_ids, duration_opt):
        super(AccumulatedHistory, self).__init__(db, osrelease_ids, component_ids, duration_opt)

    def generate_chart_data(self, chart_data, dates):
        last_value = 0
        reports = iter(chart_data)
        report = next(reports)

        for date in dates:
            if date < report[0]:
                yield (date,last_value)
            else:
                last_value = report[1]
                yield report
                report = next(reports)

    def get_min_date(self):
        hist_mindate = self.db.session.query(func.min(self.hist_column).label("value")).one()
        return hist_mindate[0] if not hist_mindate[0] is None else datetime.date.today()

    def query_all(self, query_obj):
        hist_dates = self.db.session.query(distinct(self.hist_column).label("time")).subquery()
        query_obj = query_obj.subquery()

        return (self.db.session.query(hist_dates.c.time,
                                 func.sum(query_obj.c.count))
                        .filter(hist_dates.c.time>=query_obj.c.time)
                        .group_by(hist_dates.c.time)
                        .order_by(hist_dates.c.time)
                    ).all()

def index(request, *args, **kwargs):
    db = pyfaf.storage.getDatabase()
    params = dict(request.REQUEST)
    params.update(kwargs)
    form = ReportOverviewForm(db, params)

    duration_opt = form.get_duration_selection()
    component_ids = form.get_component_selection()

    reports = ((name, AccumulatedHistory(db,
                                         ids,
                                         component_ids,
                                         duration_opt).report_counts())
                for ids, name in form.get_release_selection())

    forward = {'reports' : reports, 'duration' : duration_opt, 'form' : form}

    return render_to_response('reports/index.html',
            forward, context_instance=RequestContext(request))

def listing(request, *args, **kwargs):
    db = pyfaf.storage.getDatabase()
    params = dict(request.REQUEST)
    params.update(kwargs)
    form = ReportFilterForm(db, params)

    statuses = (db.session.query(Report.id, literal('NEW').label('status'))
        .filter(Report.problem_id==None).subquery())

    if form.get_status_selection() == 'fixed':
        statuses = (db.session.query(Report.id,
                literal('FIXED').label('status'))
            .filter(Report.problem_id!=None).subquery())

    opsysrelease_id = form.os_release_id
    reports = (db.session.query(Report.id, literal(0).label('rank'),
            statuses.c.status, Report.first_occurence.label('created'),
            Report.last_occurence.label('last_change'),
            OpSysComponent.name.label('component'))
        .join(ReportOpSysRelease)
        .join(OpSysComponent)
        .filter(statuses.c.id==Report.id)
        .filter((ReportOpSysRelease.opsysrelease_id==opsysrelease_id) |
            (opsysrelease_id==-1))
        .order_by(desc('last_change')))

    component_ids = form.get_component_selection()
    if component_ids:
        reports = reports.filter(Report.component_id.in_(component_ids))

    reports = reports.all()

    i = 1
    for rep in reports:
        rep.rank = i
        i += 1

    reports = paginate(reports, request)
    forward = {'reports' : reports,
               'form'  : form}

    return render_to_response('reports/list.html',
        forward, context_instance=RequestContext(request))

def load_packages(db, report_id, package_table):
    build_fn = lambda prefix, column : (db.session.query(package_table.id.label('%sid' % (prefix)),
                                                           Package.id.label('%spackage_id' % (prefix)),
                                                           Package.name.label('%sname' % (prefix)),
                                                           Build.version.label('%sversion' % (prefix)),
                                                           Build.release.label('%srelease' % (prefix)),
                                                           Build.epoch.label('%sepoch' % (prefix)))
                            .filter(Build.id==Package.build_id)
                            .filter(package_table.report_id==report_id)
                            .filter(Package.id==column)
                            .subquery())

    installed_packages = build_fn("i", package_table.installed_package_id)
    running_packages = build_fn("r", package_table.running_package_id)

    return (db.session.query( package_table.id,
                              installed_packages.c.ipackage_id, running_packages.c.rpackage_id,
                              installed_packages.c.iname,       running_packages.c.rname,
                              installed_packages.c.iversion,    running_packages.c.rversion,
                              installed_packages.c.irelease,    running_packages.c.rrelease,
                              installed_packages.c.iepoch,      running_packages.c.repoch,
                              package_table.count)
        .outerjoin(installed_packages, package_table.id==installed_packages.c.iid)
        .outerjoin(running_packages, package_table.id==running_packages.c.rid)
        .filter(package_table.report_id==report_id)
        .filter((installed_packages.c.iid!=None) | (running_packages.c.rid!=None))
        .all())

def item(request, report_id):
    db = pyfaf.storage.getDatabase()
    report = (db.session.query(Report, OpSysComponent, OpSys)
        .join(OpSysComponent)
        .join(OpSys)
        .filter(Report.id==report_id)
        .first())

    history_select = lambda table : (db.session.query(table).
        filter(table.report_id==report_id)
        .all())

    daily_history = history_select(ReportHistoryDaily)
    weekly_history = history_select(ReportHistoryWeekly)
    monthly_history = history_select(ReportHistoryMonthly)

    packages = load_packages(db, report_id, ReportPackage)
    related_packages = load_packages(db, report_id, ReportRelatedPackage)

    return render_to_response('reports/item.html',
                                {'report': report,
                                 'daily_history': daily_history,
                                 'weekly_history': weekly_history,
                                 'monthly_history': monthly_history,
                                 'packages': packages,
                                 'related_packages': related_packages,
                                 'backtrace': report[0].backtraces[0].frames},
                                context_instance=RequestContext(request))

@csrf_exempt
def new(request):
    if request.method == 'POST':
        form = NewReportForm(request.POST, request.FILES)
        if form.is_valid():
            report = form.cleaned_data['file']['converted']
            try:
                known = ureport.is_known(report, pyfaf.storage.Database())
            except:
                known = False

            spool_dir = pyfaf.config.get('Report.SpoolDirectory')
            fname = str(uuid.uuid4())
            with open(os.path.join(spool_dir, 'incoming', fname), 'w') as fil:
                fil.write(form.cleaned_data['file']['json'])

            if 'application/json' in request.META.get('HTTP_ACCEPT'):
                response = {'known': known}
                return HttpResponse(json.dumps(response),
                    mimetype='application/json')

            return render_to_response('reports/success.html',
                {'report': report, 'known': known},
                context_instance=RequestContext(request))
        else:
            if 'application/json' in request.META.get('HTTP_ACCEPT'):
                return HttpResponse(status=400, mimetype='application/json')

            return render_to_response('reports/new.html', {'form': form},
                context_instance=RequestContext(request))
    else:
        form = NewReportForm()

    return render_to_response('reports/new.html', {'form': form},
        context_instance=RequestContext(request))
