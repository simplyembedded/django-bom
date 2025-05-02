from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.contrib import messages
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.core.mail import send_mail
from django.conf import settings
import bom.state_diagram_builder as diagrams
from bom import constants
from bom.models import (
    PartClassWorkflowCompletedTransition,
    PartClassWorkflowStateTransition,
    PartClassWorkflowState,
    PartClassWorkflow
)
from bom.forms import (
    PartClassWorkflowStateChangeForm,
    ChangeStateAssignedUsersForm,
    CreatePartClassWorkflowTransitionForm,
)

def send_pcba_bom_update(subpart, part_revision, update_type):
    subject = '[IndaBOM] PCBA BOM Change Notification'
    message = f'BOM for PCBA "{part_revision}" has been modified.' \
                + f'The associated subpart is "{subpart}". Update type: {update_type}.' \
                + "\n\n\n\n Simply Embedded"
    recipients = ['']
    send_mail(subject, message, from_email='admin@simplyembedded.ca', 
            recipient_list=recipients, fail_silently=True)

def validate_transition_forms(request, workflow_form):
    result = {
        'has_final_state': False,
        'has_initial_state': False,
        'valid_transitions': []
    }

    for i in range(constants.NUMBER_WORKFLOW_TRANSITIONS_MAX):
        transition_form = CreatePartClassWorkflowTransitionForm(request.POST, prefix="trans{}".format(i))
        if transition_form.is_valid():
            print("got a valid")
            result['valid_transitions'].append(transition_form.cleaned_data)
            if transition_form.cleaned_data['target_state'].is_final_state:
                result['has_final_state'] = True
            if transition_form.cleaned_data['source_state'] == PartClassWorkflowState.objects.filter(id=workflow_form.data['initial_state']).first():
                result['has_initial_state'] = True

    return result


def get_transitions_error(valid_transition_results):
    if not valid_transition_results['has_final_state']:
        return "Must be able to transition to a final state."
    if not valid_transition_results['has_initial_state']:
        return "Transitions must contain the workflow's initial state."
    if len(valid_transition_results['valid_transitions']) == 0:
        return "You cannot create a workflow without defining any state transitions."
    return False


def edit_existing_workflow(request, form):
    workflow_id = request.POST.get('editing_existing_workflow') # this input stores the id in the request for simplicity
    existing_workflow = PartClassWorkflow.objects.filter(id=workflow_id).first()
    if len(form.data['name']) > 0:
        existing_workflow.name = form.data['name']
    else:
        print('The workflow name cannot be blank.')
        messages.error(request, 'The workflow name cannot be blank.')
        return False

    if len(form.data['initial_state']) > 0:
        existing_workflow.initial_state = PartClassWorkflowState.objects.filter(id=form.data['initial_state']).first()
    else:
        print('An initial state must be selected.')
        messages.error(request, 'An initial state must be selected.')
        return False

    valid_transition_results = validate_transition_forms(request, form)
    transitions_error_msg = get_transitions_error(valid_transition_results)
    if transitions_error_msg:
        print(transitions_error_msg)
        messages.error(request, transitions_error_msg)
        return False

    # Delete all existing transitions to recreate them.
    PartClassWorkflowStateTransition.objects.filter(workflow=existing_workflow).delete()
    create_transitions(valid_transition_results['valid_transitions'], workflow_id)
    existing_workflow.description = form.data['description']
    existing_workflow.save()
    return True


def validate_new_workflow_state(workflow_state_form):
    valid_results = {'is_valid': True}

    if not workflow_state_form.is_valid():
        valid_results['is_valid'] = False
        error_msg = 'Error creating new state. Missing required fields:  '
        for error in workflow_state_form.errors:
            error_msg += f'{error}, '
        valid_results['error_msg'] = error_msg

    return valid_results


def create_transitions(transitions, workflow_id):
    for transition in transitions:
        try:
            source_state = transition['source_state']
            target_state = transition['target_state']
            workflow = PartClassWorkflow.objects.filter(id=workflow_id).first()

            new_transition = PartClassWorkflowStateTransition(
                workflow=workflow,
                source_state=source_state,
                target_state=target_state,
                direction_in_workflow='forward'
            )
            new_transition.save()

            opposite_transition = PartClassWorkflowStateTransition(
                workflow=workflow,
                source_state=target_state,
                target_state=source_state,
                direction_in_workflow='backward'
            )
            opposite_transition.save()
        except:
            pass


def validate_new_workflow(request, workflow_form):
    valid_results = { 'is_valid': True }

    if not workflow_form.is_valid():
        valid_results['is_valid'] = False
        valid_results['error_msg'] = "Invalid entries for workflow"
        return valid_results

    valid_transition_results = validate_transition_forms(request, workflow_form)
    transitions_error_msg = get_transitions_error(valid_transition_results)
    if transitions_error_msg:
        valid_results['is_valid'] = False
        valid_results['error_msg'] = transitions_error_msg
    # if not valid_transition_results['has_final_state']:
    #     valid_results['is_valid'] = False
    #     valid_results['error_msg'] = "Must be able to transition to a final state."
    #
    # if not valid_transition_results['has_initial_state']:
    #     valid_results['is_valid'] = False
    #     valid_results['error_msg'] = "Transitions must contain the workflow's initial state."
    #
    # if len(valid_transition_results['valid_transitions']) == 0:
    #     valid_results['is_valid'] = False
    #     valid_results['error_msg'] = "You cannot create a workflow without defining any state transitions."

    valid_results['valid_transitions'] = valid_transition_results['valid_transitions']

    return valid_results


def get_part_workflow_context(request, workflow_instance):
    context = {}
    context['all_assigned_users'] = workflow_instance.currently_assigned_users.all()
    if len(context['all_assigned_users']) == 0:
        context['all_assigned_users'] = workflow_instance.current_state.assigned_users.all()

    context['is_assigned_user'] = request.user in context['all_assigned_users']

    all_forward_transitions = PartClassWorkflowStateTransition.objects.filter(
        workflow=workflow_instance.workflow,
        direction_in_workflow='forward'
    )

    context['workflow_str_lines'] = diagrams.workflow_str(
        initial_state=workflow_instance.workflow.initial_state,
        forward_transitions=all_forward_transitions
    )

    context['current_forward_transitions'] = all_forward_transitions.filter(
        source_state=workflow_instance.current_state
    )

    context['current_backward_transitions'] = PartClassWorkflowStateTransition.objects.filter(
        workflow=workflow_instance.workflow,
        source_state=workflow_instance.current_state,
        direction_in_workflow='backward'
    )

    if workflow_instance.current_state.is_final_state:
        context['submit_state_form'] = PartClassWorkflowStateChangeForm(final_transition=True)
    else:
        context['submit_state_form'] = PartClassWorkflowStateChangeForm(forward_transitions=context['current_forward_transitions'])

    if context['current_backward_transitions'] and len(context['current_backward_transitions']) > 0:
        context['reject_state_form'] = PartClassWorkflowStateChangeForm(backward_transitions=context['current_backward_transitions'])

    if request.user.is_superuser or request.user.bom_profile().role == 'A' or context['is_assigned_user']:
        context['change_assigned_users_form'] = ChangeStateAssignedUsersForm()
        context['change_state_form_action'] = reverse('bom:part-info', kwargs={'part_id': workflow_instance.part.id})

    return context


def send_new_task_email(message_context, fail_silently=True):
    html_message = render_to_string('bom/workflow_email_template.html', message_context)
    plain_message = strip_tags(html_message)

    send_mail(
        subject=f"[IndaBOM] New Task For Part {message_context['part']}!",
        message=plain_message,
        from_email=settings.EMAIL_HOST_USER,
        recipient_list=[message_context['assigned_user'].email],
        html_message=html_message,
        fail_silently=True,
    )


def change_assigned_users_and_refresh(request, workflow_instance):
    change_assigned_users_form = ChangeStateAssignedUsersForm(request.POST)
    if not change_assigned_users_form.is_valid():
        return HttpResponse("Error: " + change_assigned_users_form.errors['assigned_users'])

    new_assigned_users = change_assigned_users_form.cleaned_data['assigned_users']
    workflow_instance.currently_assigned_users.set(new_assigned_users)

    if change_assigned_users_form.cleaned_data['notify_new_users']:
        for assigned_user in workflow_instance.currently_assigned_users.all():
            send_new_task_email(
                message_context = {
                    'assigned_user': assigned_user,
                    'part': workflow_instance.part,
                    'previous_assigned_user': request.user.get_full_name(),
                    'comments': change_assigned_users_form.cleaned_data['comments'],
                    'transition_name': workflow_instance.current_state.name,
                    'part_info_url': f'http://{request.get_host()}/bom/part/{workflow_instance.part.id}/#workflow'
                }
            )

    return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': workflow_instance.part.id})+'#workflow')


def change_workflow_state_and_refresh(request, workflow_instance):
    change_state_form = PartClassWorkflowStateChangeForm(request.POST)
    if not change_state_form.is_valid():
        messages.error(request, f"An error occured: {change_state_form.errors['transition']}")

    if change_state_form.cleaned_data['transition'] is None and not workflow_instance.current_state.is_final_state:
        messages.error(request, "Error, please select a transition")
        return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': workflow_instance.part.id})+'#workflow')

    selected_transition = change_state_form.cleaned_data['transition']
    comments = change_state_form.cleaned_data['comments']

    if selected_transition is None and not workflow_instance.current_state.is_final_state:
        messages.error(request, "Error, please select a transition")
        return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': workflow_instance.part.id})+'#workflow')

    if change_state_form.cleaned_data['notifying_next_users'] and not selected_transition.source_state.is_final_state:
        for assigned_user in selected_transition.source_state.assigned_users.all():
            send_new_task_email(
                message_context = {
                    'assigned_user': assigned_user,
                    'part': workflow_instance.part,
                    'previous_assigned_user': request.user.get_full_name(),
                    'comments': comments,
                    'transition_name': selected_transition.target_state.name,
                    'part_info_url': f'http://{request.get_host()}/bom/part/{workflow_instance.part.id}/#workflow'
                }
            )

    completed_transition = PartClassWorkflowCompletedTransition(
        transition=selected_transition,
        completed_by=request.user,
        comments=comments,
        part=workflow_instance.part
    )

    completed_transition.save()

    if workflow_instance.current_state.is_final_state and 'submit-workflow-state' in request.POST:
        workflow_instance.delete()
        messages.success(request, f"Workflow for {workflow_instance.part} completed!")
        return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': workflow_instance.part.id}))

    workflow_instance.current_state = selected_transition.target_state
    workflow_instance.currently_assigned_users.set(selected_transition.target_state.assigned_users.all())
    workflow_instance.save()
    return HttpResponseRedirect(reverse('bom:part-info', kwargs={'part_id': workflow_instance.part.id})+'#workflow')
