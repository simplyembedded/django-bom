import csv
import logging
import operator
from functools import reduce
from json import dumps, loads
import xmlrpc.client

from ..settings import ODOO_DB, ODOO_COMMON_URL, ODOO_PASSWORD, ODOO_URL, ODOO_USERNAME, ODOO_OBJECT_URL
from ..odoo_comm import authenticate_odoo, account_for_001_010, add_subparts_to_bom_views, bom_odoo_creation


from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import IntegrityError
from django.db.models import ProtectedError, Q, prefetch_related_objects
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.encoding import smart_str
from django.utils.text import smart_split
from django.views.generic.base import TemplateView

from social_django.models import UserSocialAuth

from bom import constants
from bom.csv_headers import (
    BOMFlatCSVHeaders,
    BOMIndentedCSVHeaders,
    CSVHeader,
    ManufacturerPartCSVHeaders,
    PartClassesCSVHeaders,
    PartsListCSVHeaders,
    PartsListCSVHeadersSemiIntelligent,
    SellerPartCSVHeaders,
)
from bom.forms import (
    AddSubpartForm,
    BOMCSVForm,
    FileForm,
    ManufacturerForm,
    ManufacturerPartForm,
    OrganizationCreateForm,
    OrganizationForm,
    OrganizationFormEditSettings,
    OrganizationNumberLenForm,
    PartClassCSVForm,
    PartClassForm,
    PartClassFormSet,
    PartClassSelectionForm,
    PartClassWorkflowForm,
    PartClassWorkflowStateForm,
    CreatePartClassWorkflowStateForm,
    CreatePartClassWorkflowTransitionForm,
    PartCSVForm,
    PartFormIntelligent,
    PartFormSemiIntelligent,
    PartInfoForm,
    PartRevisionForm,
    PartRevisionNewForm,
    SellerPartForm,
    SubpartForm,
    UploadBOMForm,
    UserAddForm,
    UserCreateForm,
    UserForm,
    UserMetaForm,
)
from bom.models import (
    Assembly,
    AssemblySubparts,
    Manufacturer,
    ManufacturerPart,
    Organization,
    Part,
    PartClass,
    PartRevision,
    SellerPart,
    Subpart,
    User,
    UserMeta,
    PartClassWorkflowState,
    PartClassWorkflowStateTransition,
    PartClassWorkflow,
    PartWorkflowInstance,
    PartClassWorkflowCompletedTransition
)
from bom.utils import check_references_for_duplicates, listify_string, prep_for_sorting_nicely, stringify_list
import json
from django.conf import settings
from bom import functions


logger = logging.getLogger(__name__)
PCBA_NUMBER_CLASS = '010'

def form_error_messages(form_errors) -> [str]:
    error_messages = []
    for k, errors in form_errors.as_data().items():
        for error_message in errors:
            error_messages.append(str(error_message.message))

@login_required(login_url="login")
def home(request):
    profile = request.user.bom_profile()
    organization = profile.organization
    if not organization:
        return HttpResponseRedirect(reverse('bom:organization-create'))

    query = request.GET.get('q', '')
    only_including_unreleased = request.GET.get('filter-unreleased', '')
    title = f'{organization.name}\'s'

    # Note that posting a PartClass selection does not include a named parameter in
    # the POST, so this case is the de facto "else" clause.
    part_class_selection_form = PartClassSelectionForm(organization=organization)

    if request.method == 'GET':
        part_class_selection_form = PartClassSelectionForm(request.GET, organization=organization)
    elif request.method == 'POST':
        if 'actions' in request.POST and 'part-action' in request.POST:
            action = request.POST.get('part-action')
            if action == 'Delete':
                part_ids = [part_id for part_id in request.POST.getlist('actions') if part_id.isdigit()]
                for part_id in part_ids:
                    try:
                        part = Part.objects.get(id=part_id, organization=organization)
                        part_number = part.full_part_number()
                        part.delete()
                        messages.success(request, f"Deleted part {part_number}")
                    except Part.DoesNotExist:
                        messages.error(request, "Can't delete part. No part found with given id {}.".format(part_id))

    if part_class_selection_form.is_valid():
        part_class = part_class_selection_form.cleaned_data['part_class']
    else:
        part_class = None

    if part_class or query:
        title += f' - Search Results'
    else:
        title += f' Part List'

    if part_class:
        parts = Part.objects.filter(Q(organization=organization) & Q(number_class__code=part_class.code))
    else:
        parts = Part.objects.filter(organization=organization)



    part_ids = list(parts.values_list('id', flat=True))
    unreleased_filter = 'and pr.configuration = "W" ' if only_including_unreleased else ""

    part_rev_query = "select max(pr.id) as id from bom_partrevision as pr " \
                     "left join bom_part as p on pr.part_id = p.id " \
                     "left join bom_partclass as pc on pc.id = p.number_class_id " \
                     "where p.id in ({}) " \
                     f"{unreleased_filter}" \
                     "group by pr.part_id " \
                     "order by pc.code, p.number_item, p.number_variation"

    part_list = ','.join(map(str, part_ids)) if len(part_ids) > 0 else "NULL"
    q = part_rev_query.format(part_list)
    part_revs = PartRevision.objects.raw(q)
    prefetch_related_objects(part_revs, 'part')
    manufacturer_parts = ManufacturerPart.objects.filter(part__in=parts)

    autocomplete_dict = {}
    for pr in part_revs:
        autocomplete_dict.update({pr.searchable_synopsis.replace('"', '').replace("'", ""): None})
        autocomplete_dict.update({pr.part.full_part_number(): None})

    for mpn in manufacturer_parts:
        if mpn.manufacturer_part_number:
            autocomplete_dict.update({mpn.manufacturer_part_number.replace('"', '').replace("'", ""): None})
        if mpn.manufacturer is not None and mpn.manufacturer.name:
            autocomplete_dict.update({mpn.manufacturer.name.replace('"', '').replace("'", ""): None})

    autocomplete = dumps(autocomplete_dict)

    if query:
        query_stripped = query.strip()

        # Parse terms separated by white space but keep together words inside of double quotes,
        # for example
        #   "Big Company Inc."
        # is parsed as 'Big Company Inc.' while
        #    Big Company Inc.
        # is parsed as 'Big' 'Company' 'Inc.'
        search_terms = query_stripped
        search_terms = list(smart_split(search_terms))
        search_terms = [search_term.replace('"', '').replace("'", "") for search_term in search_terms]
        noqoutes_query = query_stripped.replace('"', '').replace("'", "")

        number_class = None
        number_item = None
        number_variation = None



        # Scan for search terms that might represent a complete or partial part number
        if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT:
            for search_term in search_terms:
                try:
                    (number_class, number_item, number_variation) = Part.parse_partial_part_number(search_term, organization, validate=False)
                except AttributeError:
                    pass

        # Query searchable_synopsis by OR'ing search terms
        part_synopsis_ids = PartRevision.objects.filter(reduce(operator.or_, (Q(searchable_synopsis__icontains=term) for term in search_terms))).values_list("part", flat=True)
        # Prepare Part.primary_manufacturer_part.manufacturer_part_number query by OR'ing search terms
        q_primary_mpn = reduce(operator.or_, (Q(primary_manufacturer_part__manufacturer_part_number__icontains=term) for term in search_terms))

        # Prepare Part.primary_manufacturer.part__manufacturer.name query by OR'ing search terms
        q_primary_mfg = reduce(operator.or_, (Q(primary_manufacturer_part__manufacturer__name__icontains=term) for term in search_terms))

        if number_class and number_item and number_variation:
            parts = parts.filter(
                Q(number_class__code=number_class, number_item=number_item, number_variation=number_variation) |
                Q(id__in=part_synopsis_ids) |
                q_primary_mpn |
                q_primary_mfg)
        elif number_class and number_item:
            parts = parts.filter(
                Q(number_class__code=number_class, number_item=number_item) |
                Q(id__in=part_synopsis_ids) |
                q_primary_mpn |
                q_primary_mfg)
        else:
            parts = parts.filter(
                Q(number_item__in=search_terms) |
                Q(id__in=part_synopsis_ids) |
                q_primary_mpn |
                q_primary_mfg)

        part_ids = list(parts.values_list('id', flat=True))
        part_list = ','.join(map(str, part_ids)) if len(part_ids) > 0 else "NULL"
        q = part_rev_query.format(part_list)
        part_revs = PartRevision.objects.raw(q)

    #info for generating csv report
    # csv_headers = organization.part_list_csv_headers()
    # csv_fieldnames = csv_headers.get_default_all()
    # seller_csv_headers = SellerPartCSVHeaders()
    # csv_part_revs = []
    # for part_rev in part_revs:
    #     row = {
    #         csv_headers.get_default('part_number'): part_rev.part.full_part_number(),
    #         csv_headers.get_default('part_revision'): part_rev.revision,
    #         csv_headers.get_default('part_manufacturer'): part_rev.part.primary_manufacturer_part.manufacturer.name if part_rev.part.primary_manufacturer_part is not None and part_rev.part.primary_manufacturer_part.manufacturer is not None else '',
    #         csv_headers.get_default('part_manufacturer_part_number'): part_rev.part.primary_manufacturer_part.manufacturer_part_number if part_rev.part.primary_manufacturer_part is not None else '',
    #     }
    #
    #     if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT:
    #         row[csv_headers.get_default('part_category')] = part_rev.part.number_class.name
    #         for field_name in csv_headers.get_default_all():
    #             if field_name not in csv_headers.get_defaults_list(['part_number', 'part_category', 'part_synopsis', 'part_revision', 'part_manufacturer', 'part_manufacturer_part_number', ]
    #                                                                + seller_csv_headers.get_default_all()):
    #                 attr = getattr(part_rev, field_name)
    #                 row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
    #
    #     else:
    #         for field_name in csv_headers.get_default_all():
    #             if field_name not in csv_headers.get_defaults_list(['part_number', 'part_synopsis', 'part_revision', 'part_manufacturer', 'part_manufacturer_part_number', ]
    #                                                                + seller_csv_headers.get_default_all()):
    #                 attr = getattr(part_rev, field_name)
    #                 row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
    #
    #     # removing any single/double quotes before passing values to js for json parsing
    #     for k, v in row.items():
    #         row[k] = str(v).replace('"', '').replace("'", "")
    #
    #     sellerparts = part_rev.part.seller_parts()
    #     if len(sellerparts) > 0:
    #         for sellerpart in part_rev.part.seller_parts():
    #             for field_name in seller_csv_headers.get_default_all():
    #                 attr = getattr(sellerpart, field_name)
    #                 row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
    #             csv_part_revs.append({k: smart_str(v) for k, v in row.items()})
    #     else:
    #         csv_part_revs.append({k: smart_str(v) for k, v in row.items()})

    if 'download' in request.GET:
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="indabom_parts_search.csv"'
        csv_headers = organization.part_list_csv_headers()
        seller_csv_headers = SellerPartCSVHeaders()
        writer = csv.DictWriter(response, fieldnames=csv_headers.get_default_all())
        writer.writeheader()
        for part_rev in part_revs:
            if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT:
                row = {
                    csv_headers.get_default('part_number'): part_rev.part.full_part_number(),
                    csv_headers.get_default('part_category'): part_rev.part.number_class.name,
                    csv_headers.get_default('part_revision'): part_rev.revision,
                    csv_headers.get_default('part_manufacturer'): part_rev.part.primary_manufacturer_part.manufacturer.name if part_rev.part.primary_manufacturer_part is not None and
                                                                                                      part_rev.part.primary_manufacturer_part.manufacturer is not None else '',
                    csv_headers.get_default('part_manufacturer_part_number'): part_rev.part.primary_manufacturer_part.manufacturer_part_number if part_rev.part.primary_manufacturer_part is not None else '',
                }
                for field_name in csv_headers.get_default_all():
                    if field_name not in csv_headers.get_defaults_list(['part_number', 'part_category', 'part_synopsis', 'part_revision', 'part_manufacturer', 'part_manufacturer_part_number', ] + seller_csv_headers.get_default_all()):
                        attr = getattr(part_rev, field_name)
                        row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
            else:
                row = {
                    csv_headers.get_default('part_number'): part_rev.part.full_part_number(),
                    csv_headers.get_default('part_revision'): part_rev.revision,
                    csv_headers.get_default('part_manufacturer'): part_rev.part.primary_manufacturer_part.manufacturer.name if part_rev.part.primary_manufacturer_part is not None and
                                                                                                      part_rev.part.primary_manufacturer_part.manufacturer is not None else '',
                    csv_headers.get_default('part_manufacturer_part_number'): part_rev.part.primary_manufacturer_part.manufacturer_part_number if part_rev.part.primary_manufacturer_part is not None else '',
                }
                for field_name in csv_headers.get_default_all():
                    if field_name not in csv_headers.get_defaults_list(['part_number', 'part_synopsis', 'part_revision', 'part_manufacturer', 'part_manufacturer_part_number', ] + seller_csv_headers.get_default_all()):
                        attr = getattr(part_rev, field_name)
                        row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
            writer.writerow({k: smart_str(v) for k, v in row.items()})
        return response

    paginator = Paginator(part_revs, 50)

    page = request.GET.get('page')
    try:
        part_revs = paginator.page(page)
    except PageNotAnInteger:
        part_revs = paginator.page(1)
    except EmptyPage:
        part_revs = paginator.page(paginator.num_pages)

    return TemplateResponse(request, 'bom/dashboard.html', locals())


@login_required
def organization_create(request):
    user = request.user
    profile = user.bom_profile()

    if user.first_name == '' and user.last_name == '':
        org_name = user.username
    else:
        org_name = user.first_name + ' ' + user.last_name

    form = OrganizationCreateForm(initial={'name': org_name, 'number_item_len': 4})
    if request.method == 'POST':
        form = OrganizationCreateForm(request.POST)
        if form.is_valid():
            organization = form.save(commit=False)
            organization.owner = user
            organization.subscription = constants.SUBSCRIPTION_TYPE_FREE
            organization.save()
            profile.organization = organization
            profile.role = constants.ROLE_TYPE_ADMIN
            profile.save()
            return HttpResponseRedirect(reverse('bom:home'))
    return TemplateResponse(request, 'bom/organization-create.html', locals())


@login_required
def search_help(request):
    return TemplateResponse(request, 'bom/search-help.html', locals())


def signup(request):
    name = 'signup'

    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            login(request, new_user, backend='django.contrib.auth.backends.ModelBackend')
            return HttpResponseRedirect(reverse('bom:home'))
    else:
        form = UserCreateForm()

    return TemplateResponse(request, 'bom/signup.html', locals())


@login_required
def bom_signup(request):
    user = request.user
    organization = user.bom_profile().organization
    title = 'Set Up Your BOM Organization'

    if organization is not None:
        return HttpResponseRedirect(reverse('bom:home'))

    return TemplateResponse(request, 'bom/bom-signup.html', locals())


@login_required
def bom_settings(request, tab_anchor=None):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization
    if organization is None:
        return HttpResponseRedirect(reverse('bom:home'))

    title = 'Settings'
    owner = organization.owner
    name = 'settings'

    part_classes = PartClass.objects.all().filter(organization=organization)
    workflows = PartClassWorkflow.objects.all()
    workflow_states = PartClassWorkflowState.objects.all()

    users_in_organization = User.objects.filter(
        id__in=UserMeta.objects.filter(organization=organization).values_list('user', flat=True)).exclude(id__in=[organization.owner.id]).order_by(
        'first_name', 'last_name', 'email')
    google_authentication = UserSocialAuth.objects.filter(user=user).first()

    organization_parts_count = Part.objects.filter(organization=organization).count()

    USER_TAB = 'user'
    ORGANIZATION_TAB = 'organization'
    INDABOM_TAB = 'indabom'

    if request.method == 'POST':
        part_class_action_ids = request.POST.getlist('actions')
        part_class_action = request.POST.get('part-class-action')
        part_class_workflow_action = request.POST.get('part-class-workflow-action')
        workflow_state_action = request.POST.get('workflow-state-action')

        if 'submit-edit-user' in request.POST:
            tab_anchor = USER_TAB
            user_form = UserForm(request.POST, instance=user)
            if user_form.is_valid():
                user = user_form.save()
            else:
                messages.error(request, user_form.errors)

        elif 'refresh-edit-user' in request.POST:
            tab_anchor = USER_TAB
            user_form = UserForm(instance=user)

        elif 'submit-add-user' in request.POST:
            tab_anchor = ORGANIZATION_TAB
            if organization.subscription == 'F':
                messages.error(request, "Error: You must have a paid account to add users.")
            else:
                user_add_form = UserAddForm(request.POST, organization=organization)
                if user_add_form.is_valid():
                    added_user_profile = user_add_form.save()
                    messages.info(request, f"Added {added_user_profile.user.first_name} {added_user_profile.user.last_name} to your organization.")
                else:
                    messages.error(request, user_add_form.errors)

        elif 'clear-add-user' in request.POST:
            tab_anchor = ORGANIZATION_TAB
            user_add_form = UserAddForm()

        elif 'submit-remove-user' in request.POST:
            tab_anchor = ORGANIZATION_TAB
            for item in request.POST:
                if 'remove_user_meta_id_' in item:
                    user_meta_id = item.partition('remove_user_meta_id_')[2]
                    try:
                        user_meta = UserMeta.objects.get(id=user_meta_id, organization=organization)
                        if user_meta.user == organization.owner:
                            messages.error(request, "Can't remove organization owner.")
                        else:
                            user_meta.organization = None
                            user_meta.role = ''
                            user_meta.save()
                    except UserMeta.DoesNotExist:
                        messages.error(request, "No user found with given id {}.".format(user_meta_id))

        elif 'submit-edit-organization' in request.POST:
            tab_anchor = ORGANIZATION_TAB
            organization_form = OrganizationFormEditSettings(request.POST, instance=organization, user=user)
            if organization_form.is_valid():
                organization_form.save()
            else:
                messages.error(request, organization_form.errors)

        elif 'refresh-edit-organization' in request.POST:
            tab_anchor = ORGANIZATION_TAB
            organization_form = OrganizationFormEditSettings(instance=organization, user=user)

        elif 'submit-number-item-len' in request.POST:
            tab_anchor = INDABOM_TAB
            organization_number_len_form = OrganizationNumberLenForm(request.POST, instance=organization)
            if organization_number_len_form.is_valid():
                organization_number_len_form.save()
            else:
                messages.error(request, organization_number_len_form.errors)

        elif 'refresh-number-item-len' in request.POST:
            tab_anchor = INDABOM_TAB
            organization_number_len_form = OrganizationNumberLenForm(organization)

        elif 'submit-part-class-create' in request.POST:
            tab_anchor = INDABOM_TAB
            part_class_form = PartClassForm(request.POST, request.FILES, organization=organization)
            if part_class_form.is_valid():
                part_class_form.save()
            else:
                messages.error(request, part_class_form.errors)

        elif 'cancel-part-class-create' in request.POST:
            tab_anchor = INDABOM_TAB
            part_class_form = PartClassForm(organization=organization)

        elif 'submit-part-class-upload' in request.POST and request.FILES.get('file') is not None:
            tab_anchor = INDABOM_TAB
            part_class_csv_form = PartClassCSVForm(request.POST, request.FILES, organization=organization)
            if part_class_csv_form.is_valid():
                for success in part_class_csv_form.successes:
                    messages.info(request, success)
                for warning in part_class_csv_form.warnings:
                    messages.warning(request, warning)
            else:
                messages.error(request, part_class_csv_form.errors)

        elif 'submit-part-class-export' in request.POST:
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="indabom_parts_search.csv"'
            csv_headers = PartClassesCSVHeaders()
            writer = csv.DictWriter(response, fieldnames=csv_headers.get_default_all())
            writer.writeheader()
            part_classes = PartClass.objects.filter(organization=organization)
            for part_class in part_classes:
                row = {}
                for field_name in csv_headers.get_default_all():
                    attr = getattr(part_class, field_name)
                    row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
                writer.writerow({k: smart_str(v) for k, v in row.items()})
            return response

        elif 'part-class-action' in request.POST and part_class_action is not None:
            if len(part_class_action_ids) <= 0:
                messages.warning(request, "No action was taken because no part classes were selected. Select part classes by checking the checkboxes below.")
            elif part_class_action == 'submit-part-class-enable-mouser':
                tab_anchor = INDABOM_TAB
                PartClass.objects.filter(id__in=part_class_action_ids).update(mouser_enabled=True)
            elif part_class_action == 'submit-part-class-disable-mouser':
                tab_anchor = INDABOM_TAB
                PartClass.objects.filter(id__in=part_class_action_ids).update(mouser_enabled=False)
            elif part_class_action == 'submit-part-class-delete':
                tab_anchor = INDABOM_TAB
                try:
                    PartClass.objects.filter(id__in=part_class_action_ids).delete()
                except PartClass.DoesNotExist as err:
                    messages.error(request, f"No part class found: {err}")
                except ProtectedError as err:
                    messages.error(request, f"Cannot delete a part class because it has parts. You must delete those parts first. {err}")
        elif 'part-class-workflow-action' in request.POST and part_class_workflow_action is not None:
            if len(part_class_action_ids) <= 0:
                messages.warning(request, "No action was taken because no workflows were selected. Select workflows by checking the checkboxes below.")
            elif part_class_workflow_action == 'submit-part-class-workflow-delete':
                try:
                    PartClassWorkflow.objects.filter(id__in=part_class_action_ids).delete()
                except PartClassWorkflow.DoesNotExist as err:
                    messages.error(request, f"No workflow found: {err}")
                except ProtectedError as err:
                    messages.error(request, f"Cannot delete workflow {err}")
        elif 'workflow-state-action' in request.POST and workflow_state_action is not None:
            if len(part_class_action_ids) <= 0:
                messages.warning(request, "No action was taken because no workflow states were selected. Select states by checking the checkboxes below.")
            elif workflow_state_action == 'submit-workflow-state-delete':
                try:
                    PartClassWorkflowState.objects.filter(id__in=part_class_action_ids).delete()
                except PartClassWorkflow.DoesNotExist as err:
                    messages.error(request, f"No workflow state found: {err}")
                except ProtectedError as err:
                    messages.error(request, f"Cannot delete workflow state {err}")
        elif 'change-number-scheme' in request.POST:
            tab_anchor = INDABOM_TAB
            if organization_parts_count > 0:
                messages.error(request, f"Please export, then delete all of your {organization_parts_count} parts before changing your organization's number scheme.")
            else:
                if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT:
                    organization.number_scheme = constants.NUMBER_SCHEME_INTELLIGENT
                    organization.number_item_len = 128
                else:
                    organization.number_scheme = constants.NUMBER_SCHEME_SEMI_INTELLIGENT
                    organization.number_item_len = 3
                organization.save()
        elif 'submit-leave-organization' in request.POST:
            if organization.owner == user:
                messages.error(request, "You are the owner of the organization. For now we're not letting owners leave their organization. This will change in the future. Contact info@indabom.com "
                                        "if you want us to manually remove you from your organization.")
            else:
                profile.organization = None
                profile.save()
                if users_in_organization == 0:
                    organization.delete()

        elif 'submit-workflow-state-create' in request.POST:
            workflow_state_form = CreatePartClassWorkflowStateForm(request.POST)
            if workflow_state_form.is_valid():
                workflow_state_form.save()
            else:
                messages.error(request, workflow_state_form.errors)

    user_form = UserForm(instance=user)
    user_add_form = UserAddForm()
    user_add_form_action = reverse('bom:settings', kwargs={'tab_anchor': ORGANIZATION_TAB})
    user_meta_form = UserMetaForm()

    organization_form = OrganizationFormEditSettings(instance=organization, user=user)
    organization_number_len_form = OrganizationNumberLenForm(instance=organization)
    part_class_form = PartClassForm(organization=organization)
    part_class_form_action = reverse('bom:settings', kwargs={'tab_anchor': INDABOM_TAB})
    part_class_csv_form = PartClassCSVForm(organization=organization)
    workflow_state_form = CreatePartClassWorkflowStateForm()
    workflow_state_form_action = reverse('bom:settings', kwargs={'tab_anchor': INDABOM_TAB})

    return TemplateResponse(request, 'bom/settings.html', locals())


@login_required
def user_meta_edit(request, user_meta_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    user_meta = get_object_or_404(UserMeta, pk=user_meta_id)
    user_meta_user = get_object_or_404(User, pk=user_meta.user.id)
    title = 'Edit User {}'.format(user_meta.user.__str__())

    if request.method == 'POST':
        user_meta_user_form = UserForm(request.POST, instance=user_meta_user)
        if user_meta_user_form.is_valid():
            user_meta_form = UserMetaForm(request.POST, instance=user_meta, organization=organization)
            if user_meta_form.is_valid():
                user_meta_user_form.save()
                user_meta_form.save()
                return HttpResponseRedirect(reverse('bom:settings', kwargs={'tab_anchor': 'organization'}))

        return TemplateResponse(request, 'bom/edit-user-meta.html', locals())

    else:
        user_meta_user_form = UserForm(instance=user_meta_user)
        user_meta_form = UserMetaForm(instance=user_meta, organization=organization)

    return TemplateResponse(request, 'bom/edit-user-meta.html', locals())


@login_required
def part_info(request, part_id, part_revision_id=None):
    tab_anchor = request.GET.get('tab_anchor', None)

    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)
    workflow_instance = PartWorkflowInstance.objects.filter(part=part).first()

    part_revision = None
    if part_revision_id is None:
        part_revision = part.latest()
    else:
        part_revision = get_object_or_404(PartRevision, pk=part_revision_id)

    try:
        selected_rev_is_latest = part_revision.revision == part.latest().revision
    except AttributeError:
        selected_rev_is_latest = False

    revisions = PartRevision.objects.filter(part=part_id).order_by('-id')

    if part.organization != organization:
        messages.error(request, "Can't access a part that is not yours!")
        return HttpResponseRedirect(reverse('bom:home'))

    qty_cache_key = str(part_id) + '_qty'
    qty = cache.get(qty_cache_key, 100)
    part_info_form = PartInfoForm(initial={'quantity': qty})
    upload_file_to_part_form = FileForm()

    if request.method == 'POST':
        part_info_form = PartInfoForm(request.POST)
        if part_info_form.is_valid():
            qty = request.POST.get('quantity', 100)

        if workflow_instance:
            if 'submit-workflow-state' in request.POST or 'reject-workflow-state' in request.POST:
                return functions.change_workflow_state_and_refresh(request, workflow_instance)

            if 'change-assigned-users' in request.POST:
                return functions.change_assigned_users_and_refresh(request, workflow_instance)

    completed_transitions = PartClassWorkflowCompletedTransition.objects.filter(part=part)
    if workflow_instance:
        workflow_context = functions.get_part_workflow_context(request, workflow_instance)

    try:
        qty = int(qty)
    except ValueError:
        qty = 100

    cache.set(qty_cache_key, qty, timeout=None)

    try:
        indented_bom = part_revision.indented(top_level_quantity=qty)
    except (RuntimeError, RecursionError):
        messages.error(request, "Error: infinite recursion in part relationship. Contact info@indabom.com to resolve.")
        indented_bom = []
    except AttributeError as err:
        messages.error(request, err)
        indented_bom = []

    try:
        flat_bom = part_revision.flat(top_level_quantity=qty)
    except (RuntimeError, RecursionError):
        messages.error(request, "Error: infinite recursion in part relationship. Contact info@indabom.com to resolve.")
        flat_bom = []
    except AttributeError as err:
        messages.error(request, err)
        flat_bom = []

    try:
        where_used = part_revision.where_used()
    except AttributeError:
        where_used = []

    try:
        mouser_parts = len(flat_bom.mouser_parts().keys()) > 0
    except AttributeError:
        mouser_parts = False

    where_used_part = part.where_used()
    seller_parts = part.seller_parts()

    return TemplateResponse(request, 'bom/part-info.html', locals())


@login_required
def part_export_bom(request, part_id=None, part_revision_id=None, flat=False, sourcing=False, sourcing_detailed=False):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    if part_id is not None:
        part = get_object_or_404(Part, pk=part_id)
        part_revision = part.latest()
    elif part_revision_id is not None:
        part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
        part = part_revision.part
    else:
        messages.error(request, "View requires part or part revision.")
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'), '/')

    if part.organization != organization:
        messages.error(request, "Cant export a part that is not yours!")
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'), '/')

    response = HttpResponse(content_type='text/csv')
    filename = f'indabom_export_{part.full_part_number()}_{"flat" if flat else "indented"}'
    response['Content-Disposition'] = f'attachment; filename="{filename}.csv'

    qty_cache_key = str(part_id) + '_qty'
    qty = cache.get(qty_cache_key, 1000)

    try:
        if flat:
            bom = part_revision.flat(top_level_quantity=qty)
        else:
            bom = part_revision.indented(top_level_quantity=qty)
    except (RuntimeError, RecursionError):
        messages.error(request, "Error: infinite recursion in part relationship. Contact info@indabom.com to resolve.")
        bom = []
    except AttributeError as err:
        messages.error(request, err)
        bom = []

    if flat:
        csv_headers = BOMFlatCSVHeaders()
    else:
        csv_headers = BOMIndentedCSVHeaders()

    csv_headers_raw = csv_headers.get_default_all()
    csv_rows = []
    for _, item in bom.parts.items():
        mapped_row = {}
        raw_row = {k: smart_str(v) for k, v in item.as_dict_for_export().items()}
        for kx, vx in raw_row.items():
            if csv_headers.get_default(kx) is None: print ("NONE", kx)
            mapped_row.update({csv_headers.get_default(kx): vx})

        if sourcing_detailed:
            for idx, sp in enumerate(item.seller_parts_for_export()):
                if f'{ManufacturerPartCSVHeaders.all_headers_defns[0]}_{idx + 1}' not in csv_headers_raw:
                    csv_headers_raw.extend([f'{h}_{idx + 1}' for h in ManufacturerPartCSVHeaders.all_headers_defns])
                    csv_headers_raw.extend([f'{h}_{idx + 1}' for h in SellerPartCSVHeaders.all_headers_defns])
                mapped_row.update({f'{k}_{idx + 1}': smart_str(v) for k, v in sp.items()})
        elif sourcing:
            for idx, mp in enumerate(item.manufacturer_parts_for_export()):
                if f'{ManufacturerPartCSVHeaders.all_headers_defns[0]}_{idx + 1}' not in csv_headers_raw:
                    csv_headers_raw.extend([f'{h}_{idx + 1}' for h in ManufacturerPartCSVHeaders.all_headers_defns])
                mapped_row.update({f'{k}_{idx + 1}': smart_str(v) for k, v in mp.items()})

        csv_rows.append(mapped_row)

    writer = csv.DictWriter(response, fieldnames=csv_headers_raw)
    writer.writeheader()
    writer.writerows(csv_rows)

    return response

# @login_required
# def part_export_bom_flat(request, part_revision_id):
#     user = request.user
#     profile = user.bom_profile()
#     organization = profile.organization
#
#     part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
#
#     if part_revision.part.organization != organization:
#         messages.error(request, "Cant export a part that is not yours!")
#         return HttpResponseRedirect(request.META.get('HTTP_REFERER'), '/')
#
#     response = HttpResponse(content_type='text/csv')
#     response['Content-Disposition'] = 'attachment; filename="{}_indabom_parts_flat.csv"'.format(
#         part_revision.part.full_part_number())
#
#     # As compared to indented bom, show all references for a subpart as a single item and
#     # don't show do_not_load status at all because it won't be clear as to which subpart reference
#     # the do_not_load refers to.
#     qty_cache_key = str(part_revision.part.id) + '_qty'
#     qty = cache.get(qty_cache_key, 1000)
#
#     try:
#         bom = part_revision.flat(top_level_quantity=qty)
#     except (RuntimeError, RecursionError):
#         messages.error(request, "Error: infinite recursion in part relationship. Contact info@indabom.com to resolve.")
#         bom = []
#     except AttributeError as err:
#         messages.error(request, err)
#         bom = []
#
#     csv_headers = BOMFlatCSVHeaders()
#     writer = csv.DictWriter(response, fieldnames=csv_headers.get_default_all())
#     writer.writeheader()
#
#     for _, item in bom.parts.items():
#         mapped_row = {}
#         raw_row = {k: smart_str(v) for k, v in item.as_dict_for_export().items()}
#         for kx, vx in raw_row.items():
#             if csv_headers.get_default(kx) is None: print ("NONE", kx)
#             mapped_row.update({csv_headers.get_default(kx): vx})
#         writer.writerow({k: smart_str(v) for k, v in mapped_row.items()})
#
#     return response


@login_required
def upload_bom(request):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    if request.method == 'POST' and 'file' in request.FILES and request.FILES['file'] is not None:
        upload_bom_form = UploadBOMForm(request.POST, organization=organization)
        if upload_bom_form.is_valid():
            bom_csv_form = BOMCSVForm(request.POST, request.FILES, parent_part=upload_bom_form.parent_part, organization=organization)
            if bom_csv_form.is_valid():
                for success in bom_csv_form.successes:
                    messages.info(request, success)
                for warning in bom_csv_form.warnings:
                    messages.info(request, warning)
            else:
                messages.error(request, bom_csv_form.errors)
        else:
            messages.error(request, upload_bom_form.errors)
    else:
        upload_bom_form = UploadBOMForm(initial={'organization': organization})
        bom_csv_form = BOMCSVForm()

    return TemplateResponse(request, 'bom/upload-bom.html', locals())


@login_required
def part_upload_bom(request, part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    try:
        parent_part = Part.objects.get(id=part_id)
    except Part.DoesNotExist:
        messages.error(request, "No part found with given part_id {}.".format(part_id))
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'), '/')

    if request.method == 'POST' and request.FILES['file'] is not None:
        bom_csv_form = BOMCSVForm(request.POST, request.FILES, parent_part=parent_part, organization=organization)
        if bom_csv_form.is_valid():
            for success in bom_csv_form.successes:
                messages.info(request, success)
            for warning in bom_csv_form.warnings:
                messages.info(request, warning)
        else:
            messages.error(request, bom_csv_form.errors)
    else:
        upload_bom_form = UploadBOMForm(initial={'organization': organization})
        bom_csv_form = BOMCSVForm()

    return HttpResponseRedirect(request.META.get('HTTP_REFERER', reverse('bom:home')), locals())


@login_required
def upload_parts_help(request):
    return TemplateResponse(request, 'bom/upload-parts-help.html', locals())


@login_required
def upload_parts(request):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization
    title = 'Upload Parts'

    if request.method == 'POST' and request.FILES['file'] is not None:
        form = PartCSVForm(request.POST, request.FILES, organization=organization)
        if form.is_valid():
            for success in form.successes:
                messages.info(request, success)
            for warning in form.warnings:
                messages.warning(request, warning)
        else:
            messages.error(request, form.errors)
    else:
        form = FileForm()
        return TemplateResponse(request, 'bom/upload-parts.html', locals())

    return HttpResponseRedirect(request.META.get('HTTP_REFERER', reverse('bom:home')))


@login_required
def export_part_list(request):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="indabom_parts.csv"'

    parts = Part.objects.filter(
        organization=organization).order_by(
        'number_class__code',
        'number_item',
        'number_variation')

    fieldnames = [
        'part_number',
        'part_synopsis',
        'part_revision',
        'part_manufacturer',
        'part_manufacturer_part_number',
    ]

    csv_headers = organization.part_list_csv_headers()
    writer = csv.DictWriter(response, fieldnames=fieldnames)
    writer.writeheader()
    for item in parts:
        try:
            row = {
                csv_headers.get_default('part_number'): item.full_part_number(),
                csv_headers.get_default('part_revision'): item.latest().revision,
                csv_headers.get_default('part_manufacturer'): item.primary_manufacturer_part.manufacturer.name if item.primary_manufacturer_part is not None and item.primary_manufacturer_part.manufacturer is not None else '',
                csv_headers.get_default('part_manufacturer_part_number'): item.primary_manufacturer_part.manufacturer_part_number if item.primary_manufacturer_part is not None and item.primary_manufacturer_part.manufacturer is not None else '',
            }
            for field_name in csv_headers.get_default_all():
                if field_name not in csv_headers.get_defaults_list(
                        ['part_number', 'part_category', 'part_synopsis', 'part_revision', 'part_manufacturer',
                         'part_manufacturer_part_number', ]):
                    attr = getattr(item, field_name)
                    row.update({csv_headers.get_default(field_name): attr if attr is not None else ''})
            writer.writerow({k: smart_str(v) for k, v in row.items()})

        except AttributeError as e:
            messages.warning(request, "No change history for part: {}. Can't export.".format(item.full_part_number()))

    return response


@login_required
def create_part_class_workflow(request, workflow_id=None): # if id given, editing existing workflow
    user = request.user
    profile = user.bom_profile()
    title = 'Create New Part Class Workflow'
    max_transitions = constants.NUMBER_WORKFLOW_TRANSITIONS_MAX
    restored_transitions = 0
    transition_forms = []

    if workflow_id:  # Editing existing workflow
        existing_workflow = PartClassWorkflow.objects.filter(id=workflow_id).first()
        editing_existing_workflow = True
        title = f"Editing Workflow '{existing_workflow.name}'"

        # fetching the workflows transitions
        workflow_form = PartClassWorkflowForm(instance=existing_workflow)
        existing_transitions = PartClassWorkflowStateTransition.objects.filter(workflow=existing_workflow, direction_in_workflow='forward')
        for i in range(len(existing_transitions)):
            prefix = f'trans{i}'
            transition_forms.append(CreatePartClassWorkflowTransitionForm(instance=existing_transitions[i], prefix=prefix))
            restored_transitions += 1
    else:
        workflow_form = PartClassWorkflowForm(initial={'name': '', 'current_state': ''})

    for i in range(restored_transitions, max_transitions):
        prefix = f'trans{i}'
        transition_forms.append(CreatePartClassWorkflowTransitionForm(prefix=prefix))

    new_state_form = CreatePartClassWorkflowStateForm()
    new_state_form_action = reverse('bom:create-part-class-workflow')

    if request.method == 'POST':
        if 'submit-workflow-state-create' in request.POST:
            workflow_state_form = CreatePartClassWorkflowStateForm(request.POST)
            valid_state_results = functions.validate_new_workflow_state(workflow_state_form)

            if valid_state_results['is_valid']:
                workflow_state_form.save()
                messages.success(request, f"State '{workflow_state_form.cleaned_data['name']}' saved!")
            else:
                messages.error(request, valid_state_results['error_msg'])
                return TemplateResponse(request, 'bom/create-part-class-workflow.html', locals())

        else: # workflow form submitted
            workflow_form = PartClassWorkflowForm(request.POST)
            if 'editing_existing_workflow' in request.POST:
                workflow_id = request.POST.get('editing_existing_workflow')
                if functions.edit_existing_workflow(request, workflow_form):
                    messages.success(request, 'Changes saved!')
                return HttpResponseRedirect(reverse('bom:part-class-workflow-edit', kwargs={'workflow_id': workflow_id}))

            valid_workflow_results = functions.validate_new_workflow(request, workflow_form)

            if not valid_workflow_results['is_valid']:
                messages.error(request, valid_workflow_results['error_msg'])
                return TemplateResponse(request, 'bom/create-part-class-workflow.html', locals())

            workflow_id = workflow_form.save().id
            functions.create_transitions(valid_workflow_results['valid_transitions'], workflow_id)
            messages.success(request, "Workflow created!")

    return TemplateResponse(request, 'bom/create-part-class-workflow.html', locals())

@login_required
def workflow_state_edit(request, state_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    state = get_object_or_404(PartClassWorkflowState, pk=state_id)
    title = f"Edit Workflow State '{state.name}'"

    if request.method == "POST":
        workflow_state_form = CreatePartClassWorkflowStateForm(request.POST, instance=state)
        if workflow_state_form.is_valid():
            workflow_state_form.save()
            messages.success(request, 'Changes saved!')
            return HttpResponseRedirect(reverse('bom:settings', kwargs={'tab_anchor': 'indabom'}))
        else:
            return TemplateResponse(request, 'bom/edit-workflow-state.html', locals())

    else:
        workflow_state_form = CreatePartClassWorkflowStateForm(instance=state)

    return TemplateResponse(request, 'bom/edit-workflow-state.html', locals())



@login_required
def create_part(request):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    title = 'Create New Part'

    PartForm = PartFormSemiIntelligent if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT else PartFormIntelligent

    if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT and PartClass.objects.count() == 0:
        messages.info(request, f'Welcome to IndaBOM! Before you create your first part, you must create your first part class. '
                               f'<a href="{reverse("bom:help")}#part-numbering" target="_blank">What is a part class?</a>')
        return HttpResponseRedirect(reverse('bom:settings', kwargs={'tab_anchor': 'indabom'}))

    if request.method == 'POST':
        part_form = PartForm(request.POST, organization=organization)
        manufacturer_form = ManufacturerForm(request.POST)
        manufacturer_part_form = ManufacturerPartForm(request.POST, organization=organization)
        part_revision_form = PartRevisionForm(request.POST)
        # Checking if part form is valid checks for number uniqueness
        if part_form.is_valid() and manufacturer_form.is_valid() and manufacturer_part_form.is_valid():
            mpn = manufacturer_part_form.cleaned_data['manufacturer_part_number']
            old_manufacturer = manufacturer_part_form.cleaned_data['manufacturer']
            new_manufacturer_name = manufacturer_form.cleaned_data['name']

            manufacturer = None
            if mpn:
                if old_manufacturer and new_manufacturer_name == '':
                    manufacturer = old_manufacturer
                elif new_manufacturer_name and new_manufacturer_name != '' and not old_manufacturer:
                    manufacturer, created = Manufacturer.objects.get_or_create(name__iexact=new_manufacturer_name, organization=organization, defaults={'name': new_manufacturer_name})
                else:
                    messages.error(request, "Either create a new manufacturer, or select an existing manufacturer.")
                    return TemplateResponse(request, 'bom/create-part.html', locals())
            elif old_manufacturer or new_manufacturer_name != '':
                messages.warning(request, "No manufacturer was selected or created, no manufacturer part number was assigned.")
            new_part = part_form.save(commit=False)
            new_part.organization = organization

            if organization.number_scheme == constants.NUMBER_SCHEME_INTELLIGENT:
                new_part.number_class = None
                new_part.number_variation = None

            if part_revision_form.is_valid():
                # Save the Part before the PartRevision, as this will again check for part
                # number uniqueness. This way if someone else(s) working concurrently is also
                # using the same part number, then only one person will succeed.
                try:
                    pr = part_revision_form.save(commit=False)
                    pr.part = new_part  # Associate PartRevision with Part
                    new_part.save()  # Database checks that the part number is still unique
                    pr.save()
                except IntegrityError as err:
                    messages.error(request, "Error! Already created a part with part number {0}-{1}-{3}}".format(new_part.number_class.code, new_part.number_item, new_part.number_variation))
                    return TemplateResponse(request, 'bom/create-part.html', locals())
            else:
                messages.error(request, part_revision_form.errors)
                return TemplateResponse(request, 'bom/create-part.html', locals())

            manufacturer_part = None
            if manufacturer is not None:
                manufacturer_part, created = ManufacturerPart.objects.get_or_create(
                    part=new_part,
                    manufacturer_part_number='' if mpn == '' else mpn,
                    manufacturer=manufacturer)

                new_part.primary_manufacturer_part = manufacturer_part
                new_part.save()

            # creating new instance of a workflow for the part.
            workflow = new_part.number_class.workflow
            if workflow:
                workflow_instance = PartWorkflowInstance(
                    part=new_part,
                    workflow=workflow,
                    current_state=workflow.initial_state
                )
                workflow_instance.save()

            return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': str(new_part.id)}))
    else:
        # Initialize organization in the form's model and in the form itself:
        part_form = PartForm(initial={'organization': organization}, organization=organization)
        part_revision_form = PartRevisionForm(initial={'revision': 1, 'organization': organization})
        manufacturer_form = ManufacturerForm(initial={'organization': organization})
        manufacturer_part_form = ManufacturerPartForm(organization=organization)

    return TemplateResponse(request, 'bom/create-part.html', locals())


@login_required
def part_edit(request, part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)
    title = 'Edit Part {}'.format(part.full_part_number())

    action = reverse('bom:part-edit', kwargs={'part_id': part_id})

    PartForm = PartFormSemiIntelligent if organization.number_scheme == constants.NUMBER_SCHEME_SEMI_INTELLIGENT else PartFormIntelligent

    if request.method == 'POST':
        form = PartForm(request.POST, instance=part, organization=organization)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part_id}))
    else:
        form = PartForm(instance=part, organization=organization)

    return TemplateResponse(request, 'bom/bom-form.html', locals())


@login_required
def manage_bom(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)

    title = 'Manage BOM for ' + part.full_part_number()

    if part.organization != organization:
        messages.error(request, "Cant access a part that is not yours!")
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'), '/')

    add_subpart_form = AddSubpartForm(initial={'count': 1, }, organization=organization, part_id=part_id)
    upload_subparts_csv_form = FileForm()

    qty_cache_key = str(part_id) + '_qty'
    qty = cache.get(qty_cache_key, 100)

    try:
        indented_bom = part_revision.indented(top_level_quantity=qty)
    except (RuntimeError, RecursionError):
        messages.error(request, "Error: infinite recursion in part relationship. Contact info@indabom.com to resolve.")
        indented_bom = []
    except AttributeError as err:
        messages.error(request, err)
        indented_bom = []

    references_seen = set()
    duplicate_references = set()
    for sp in part_revision.assembly.subparts.all():
        check_references_for_duplicates(sp.reference, references_seen, duplicate_references)

    if len(duplicate_references) > 0:
        sorted_duplicate_references = sorted(duplicate_references, key=prep_for_sorting_nicely)
        messages.warning(request, "Warning: The following BOM references are associated with multiple parts: " + str(sorted_duplicate_references))

    return TemplateResponse(request, 'bom/part-revision-manage-bom.html', locals())


@login_required
def part_delete(request, part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    try:
        part = Part.objects.get(id=part_id)
    except Part.DoesNotExist:
        messages.error(request, "No part found with given part_id {}.".format(part_id))
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'), '/')

    part.delete()

    return HttpResponseRedirect(reverse('bom:home'))


@login_required
def add_subpart(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)

    if request.method == 'POST':
        add_subpart_form = AddSubpartForm(request.POST, organization=organization, part_id=part_id)
        if add_subpart_form.is_valid():
            subpart_part = add_subpart_form.subpart_part
            reference = add_subpart_form.cleaned_data['reference']
            dnl = add_subpart_form.cleaned_data['do_not_load']
            count = add_subpart_form.cleaned_data['count']
            alternates = add_subpart_form.cleaned_data['alternates']

            first_level_bom = part_revision.assembly.subparts.filter(part_revision=subpart_part, do_not_load=dnl)

            if first_level_bom.count() > 0:
                new_part = first_level_bom[0]
                new_part.count += count
                if len(alternates) > 0:
                    new_part.alternates.set(alternates)
                if reference:
                    new_part.reference = new_part.reference + ', ' + reference
                new_part.save()
            else:
                new_part = Subpart.objects.create(
                    part_revision=subpart_part,
                    count=count,
                    reference=reference,
                    do_not_load=dnl,
                )

                if len(alternates) > 0:
                    new_part.alternates.set(alternates)

                if part_revision.assembly is None:
                    part_revision.assembly = Assembly.objects.create()
                    part_revision.save()

                AssemblySubparts.objects.create(assembly=part_revision.assembly, subpart=new_part)

            if part_revision.part.number_class.code == PCBA_NUMBER_CLASS:
                functions.send_pcba_bom_update(subpart_part, part_revision, 'Add')
            info_msg = "Added subpart "
            if reference:
                info_msg += ' ' + reference
            info_msg += " {} to part {}".format(subpart_part, part_revision)
            messages.info(request, info_msg)

        else:
            messages.error(request, add_subpart_form.errors)

    return HttpResponseRedirect(reverse('bom:part-manage-bom', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id}))


@login_required
def remove_subpart(request, part_id, part_revision_id, subpart_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    subpart = get_object_or_404(Subpart, pk=subpart_id)
    subpart.delete()
    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    if part_revision.part.number_class.code == PCBA_NUMBER_CLASS:
        functions.send_pcba_bom_update(subpart, part_revision, 'Delete')
    return HttpResponseRedirect(
        reverse('bom:part-manage-bom', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id}))


@login_required
def part_class_edit(request, part_class_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part_class = get_object_or_404(PartClass, pk=part_class_id)
    title = 'Edit Part Class {}'.format(part_class.__str__())

    if request.method == 'POST':
        part_class_form = PartClassForm(request.POST, instance=part_class, organization=organization)
        if part_class_form.is_valid():
            part_class_form.save()
            return HttpResponseRedirect(reverse('bom:settings', kwargs={'tab_anchor': 'indabom'}))

        else:
            return TemplateResponse(request, 'bom/edit-part-class.html', locals())

    else:
        part_class_form = PartClassForm(instance=part_class, organization=organization)

    return TemplateResponse(request, 'bom/edit-part-class.html', locals())


@login_required
def edit_subpart(request, part_id, part_revision_id, subpart_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization
    action = reverse('bom:part-edit-subpart', kwargs={'part_id': part_id, 'subpart_id': subpart_id, 'part_revision_id': part_revision_id})

    part = get_object_or_404(Part, pk=part_id)
    subpart = get_object_or_404(Subpart, pk=subpart_id)
    title = "Edit Subpart"
    h1 = "{} {}".format(subpart.part_revision.part.full_part_number(), subpart.part_revision.synopsis())

    if request.method == 'POST':
        form = SubpartForm(request.POST, instance=subpart, organization=organization, part_id=subpart.part_revision.part.id)
        if form.is_valid():
            reference_list = listify_string(form.cleaned_data['reference'])
            count = form.cleaned_data['count']
            form.save()
            part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
            if part_revision.part.number_class.code == PCBA_NUMBER_CLASS:
                functions.send_pcba_bom_update(subpart, part_revision, 'Edit')
            return HttpResponseRedirect(reverse('bom:part-manage-bom', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id}))
        else:
            return TemplateResponse(request, 'bom/bom-form.html', locals())

    else:
        form = SubpartForm(instance=subpart, organization=organization, part_id=subpart.part_revision.part.id)

    return TemplateResponse(request, 'bom/bom-form.html', locals())


@login_required
def remove_all_subparts(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    part_revision.assembly.subparts.all().delete()
    if part_revision.part.number_class.code == PCBA_NUMBER_CLASS:
        functions.send_pcba_bom_update('(All)', part_revision, 'Delete All')

    return HttpResponseRedirect(reverse('bom:part-manage-bom', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id}))


###################################################################### SYNC ODOO FUNCTION ##########################################################################

@login_required
def sync_bom_to_odoo(request, part_id, part_revision_id):
    
    # print(part_id)
    # print(part_revision_id)
    
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    
    message_for_user = bom_odoo_creation(part_revision, request)
    
    if  message_for_user == True:   # success
        messages.info(request, "A BOM was successfully created in Odoo.")
    
    else: # fail/False. Something went wrong
        messages.error(request, "BOM Creation in Odoo failed.")
      
    # else:  # means the return value is an integer (the num of subparts not found) --> Bom was created but not all subparts were found/added
    #     messages.warning(request, f"A BOM was successfully created in Odoo. However, {message_for_user} subpart(s) were not found.")
        
    return HttpResponseRedirect(reverse('bom:part-manage-bom', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id})) # constant
    
#############################################################################################################################################################

@login_required
def add_sellerpart(request, manufacturer_part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization
    title = 'Add Seller Part'

    manufacturer_part = get_object_or_404(ManufacturerPart, pk=manufacturer_part_id)
    title = "Add Seller Part to {}".format(manufacturer_part)

    if request.method == 'POST':
        form = SellerPartForm(request.POST, manufacturer_part=manufacturer_part, organization=organization)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect(
                reverse('bom:part-info', kwargs={'part_id': manufacturer_part.part.id}) + '?tab_anchor=sourcing')
    else:
        form = SellerPartForm(organization=organization)

    return TemplateResponse(request, 'bom/bom-form.html', locals())


@login_required
def add_manufacturer_part(request, part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)
    title = 'Add Manufacturer Part to {}'.format(part.full_part_number())

    if request.method == 'POST':
        manufacturer_form = ManufacturerForm(request.POST)
        manufacturer_part_form = ManufacturerPartForm(request.POST, organization=organization)
        if manufacturer_form.is_valid() and manufacturer_part_form.is_valid():
            manufacturer_part_number = manufacturer_part_form.cleaned_data['manufacturer_part_number']
            manufacturer = manufacturer_part_form.cleaned_data['manufacturer']
            new_manufacturer_name = manufacturer_form.cleaned_data['name']
            if manufacturer is None and new_manufacturer_name == '':
                messages.error(request, "Must either select an existing manufacturer, or enter a new manufacturer name.")
                return TemplateResponse(request, 'bom/add-manufacturer-part.html', locals())

            if new_manufacturer_name != '' and new_manufacturer_name is not None:
                manufacturer, created = Manufacturer.objects.get_or_create(name__iexact=new_manufacturer_name, organization=organization, defaults={'name': new_manufacturer_name})
                manufacturer_part_form.cleaned_data['manufacturer'] = manufacturer

            manufacturer_part, created = ManufacturerPart.objects.get_or_create(part=part, manufacturer_part_number=manufacturer_part_number, manufacturer=manufacturer)

            if part.primary_manufacturer_part is None and manufacturer_part is not None:
                part.primary_manufacturer_part = manufacturer_part
                part.save()

            return HttpResponseRedirect(
                reverse('bom:part-info', kwargs={'part_id': str(part.id)}) + '?tab_anchor=sourcing')
        else:
            messages.error(request, "{}".format(manufacturer_form.is_valid()))
            messages.error(request, "{}".format(manufacturer_part_form.is_valid()))
    else:
        default_mfg = Manufacturer.objects.filter(organization=organization, name__iexact=organization.name).first()
        manufacturer_form = ManufacturerForm(initial={'organization': organization})
        manufacturer_part_form = ManufacturerPartForm(organization=organization, initial={'manufacturer_part_number': part.full_part_number(), 'manufacturer': default_mfg})

    return TemplateResponse(request, 'bom/add-manufacturer-part.html', locals())


@login_required
def manufacturer_part_edit(request, manufacturer_part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    title = 'Edit Manufacturer Part'

    manufacturer_part = get_object_or_404(ManufacturerPart, pk=manufacturer_part_id)
    part = manufacturer_part.part

    if request.method == 'POST':
        manufacturer_part_form = ManufacturerPartForm(request.POST, instance=manufacturer_part, organization=organization)
        manufacturer_form = ManufacturerForm(request.POST, instance=manufacturer_part.manufacturer)
        if manufacturer_part_form.is_valid() and manufacturer_form.is_valid():
            manufacturer_part_number = manufacturer_part_form.cleaned_data.get('manufacturer_part_number')
            manufacturer = manufacturer_part_form.cleaned_data.get('manufacturer', None)
            new_manufacturer_name = manufacturer_form.cleaned_data.get('name', '')

            if manufacturer is None and new_manufacturer_name == '':
                messages.error(request, "Must either select an existing manufacturer, or enter a new manufacturer name.")
                return TemplateResponse(request, 'bom/edit-manufacturer-part.html', locals())

            new_manufacturer = None
            if new_manufacturer_name != '' and new_manufacturer_name is not None:
                new_manufacturer, created = Manufacturer.objects.get_or_create(name__iexact=new_manufacturer_name, organization=organization, defaults={'name': new_manufacturer_name})
                manufacturer_part = manufacturer_part_form.save(commit=False)
                manufacturer_part.manufacturer = new_manufacturer
                manufacturer_part.save()
            else:
                manufacturer_part = manufacturer_part_form.save()

            if part.primary_manufacturer_part is None and manufacturer_part is not None:
                part.primary_manufacturer_part = manufacturer_part
                part.save()
            return HttpResponseRedirect(
                reverse('bom:part-info', kwargs={'part_id': manufacturer_part.part.id}) + '?tab_anchor=sourcing')
        else:
            messages.error(request, manufacturer_part_form.errors)
            messages.error(request, manufacturer_form.errors)
    else:
        if manufacturer_part.manufacturer is None:
            manufacturer_form = ManufacturerForm(instance=manufacturer_part.manufacturer, initial={'organization': organization})
        else:
            manufacturer_form = ManufacturerForm(initial={'organization': organization})

        manufacturer_part_form = ManufacturerPartForm(instance=manufacturer_part, organization=organization, )

    return TemplateResponse(request, 'bom/edit-manufacturer-part.html', locals())


@login_required
def manufacturer_part_delete(request, manufacturer_part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    manufacturer_part = get_object_or_404(ManufacturerPart, pk=manufacturer_part_id)
    part = manufacturer_part.part
    manufacturer_part.delete()

    return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part.id}) + '?tab_anchor=sourcing')


@login_required
def sellerpart_edit(request, sellerpart_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    title = "Edit Seller Part"
    action = reverse('bom:sellerpart-edit', kwargs={'sellerpart_id': sellerpart_id})
    sellerpart = get_object_or_404(SellerPart, pk=sellerpart_id)

    if request.method == 'POST':
        form = SellerPartForm(request.POST, instance=sellerpart, organization=organization)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': sellerpart.manufacturer_part.part.id}) + '?tab_anchor=sourcing')
    else:
        form = SellerPartForm(instance=sellerpart, organization=organization)

    return TemplateResponse(request, 'bom/bom-form.html', locals())


@login_required
def sellerpart_delete(request, sellerpart_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    sellerpart = get_object_or_404(SellerPart, pk=sellerpart_id)
    part = sellerpart.manufacturer_part.part
    sellerpart.delete()
    return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part.id}) + '?tab_anchor=sourcing')


@login_required
def part_revision_release(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)
    workflow_instance = PartWorkflowInstance.objects.filter(part=part)
    if workflow_instance:
        messages.error(request, "Cannot release part until it's workflow is finished!")
        return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part.id}))

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    action = reverse('bom:part-revision-release', kwargs={'part_id': part.id, 'part_revision_id': part_revision.id})
    title = 'Promote {} Rev {} {} from <b>Working</b> to <b>Released</b>?'.format(part.full_part_number(), part_revision.revision, part_revision.synopsis())

    subparts = part_revision.assembly.subparts.filter(part_revision__configuration="W")
    release_warning = subparts.count() > 0

    if request.method == 'POST':
        part_revision.configuration = 'R'
        part_revision.save()
        return HttpResponseRedirect(reverse('bom:part-info-history', kwargs={'part_id': part.id, 'part_revision_id': part_revision.id}))

    return TemplateResponse(request, 'bom/part-revision-release.html', locals())


@login_required
def part_revision_revert(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    part_revision.configuration = 'W'
    part_revision.save()
    return HttpResponseRedirect(reverse('bom:part-info-history', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id}))


@login_required
def part_revision_new(request, part_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)
    title = 'New Revision for {}'.format(part.full_part_number())
    action = reverse('bom:part-revision-new', kwargs={'part_id': part_id})

    latest_revision = part.latest()
    next_revision_number = latest_revision.next_revision() if latest_revision else None

    all_part_revisions = part.revisions()
    all_used_part_revisions = PartRevision.objects.filter(part=part)
    used_in_subparts = Subpart.objects.filter(part_revision__in=all_used_part_revisions)
    used_in_assembly_ids = AssemblySubparts.objects.filter(subpart__in=used_in_subparts).values_list('assembly', flat=True)
    all_used_in_prs = PartRevision.objects.filter(assembly__in=used_in_assembly_ids)
    used_part_revisions = all_used_in_prs.filter(configuration='W')

    if request.method == 'POST':
        part_revision_new_form = PartRevisionNewForm(request.POST, part=part, revision=next_revision_number, assembly=latest_revision.assembly)
        if part_revision_new_form.is_valid():
            new_part_revision = part_revision_new_form.save()

            revisions_to_roll = request.POST.getlist('roll')
            # TODO: could optimize this, but probably shouldn't get too crazy so may be fine...
            for r_id in revisions_to_roll:
                subparts = PartRevision.objects.get(id=r_id).assembly.subparts \
                    .filter(part_revision__in=all_part_revisions)
                subparts.update(part_revision=new_part_revision)

            if part_revision_new_form.cleaned_data['copy_assembly']:
                old_subparts = latest_revision.assembly.subparts.all() if latest_revision.assembly is not None else None
                new_assembly = latest_revision.assembly if latest_revision.assembly is not None else Assembly()
                new_assembly.pk = None
                new_assembly.save()

                part_revision_new_form.cleaned_data['assembly'] = new_assembly

                new_part_revision.assembly = new_assembly
                new_part_revision.save()
                for sp in old_subparts:
                    new_sp = sp
                    new_sp.pk = None
                    new_sp.save()
                    AssemblySubparts.objects.create(assembly=new_assembly, subpart=new_sp)
            return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part_id}))

    else:
        if latest_revision:
            messages.info(request, 'New revision automatically incremented to `{}` from your last revision `{}`.'.format(next_revision_number, latest_revision.revision))
            latest_revision.revision = next_revision_number  # use updated object to populate form but don't save changes
            part_revision_new_form = PartRevisionNewForm(instance=latest_revision)
        else:
            part_revision_new_form = PartRevisionNewForm()

    return TemplateResponse(request, 'bom/part-revision-new.html', locals())


@login_required
def part_revision_edit(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)
    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    title = 'Edit {} Rev {}'.format(part.full_part_number(), part_revision.revision)

    action = reverse('bom:part-revision-edit', kwargs={'part_id': part_id, 'part_revision_id': part_revision_id})

    if request.method == 'POST':
        form = PartRevisionForm(request.POST, instance=part_revision)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part_id}))
    else:
        form = PartRevisionForm(instance=part_revision)

    return TemplateResponse(request, 'bom/part-revision-edit.html', locals())


@login_required
def part_revision_delete(request, part_id, part_revision_id):
    user = request.user
    profile = user.bom_profile()
    organization = profile.organization

    part = get_object_or_404(Part, pk=part_id)

    if profile.role != 'A':
        messages.error(request, 'Only an admin can perform this action.')
        return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part_id}))

    part_revision = get_object_or_404(PartRevision, pk=part_revision_id)
    part = part_revision.part
    part_revision.delete()

    if part.revisions().count() == 0:
        part.delete()
        messages.info(request, 'Deleted {}.'.format(part.full_part_number()))
        return HttpResponseRedirect(reverse('bom:home'))

    messages.info(request, 'Deleted {} Rev {}.'.format(part.full_part_number(), part_revision.revision))

    return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': part.id}))


class Help(TemplateView):
    name = 'help'
    template_name = f'bom/{name}.html'

    def get_context_data(self, *args, **kwargs):
        context = super(Help, self).get_context_data(**kwargs)
        context['name'] = self.name
        return context