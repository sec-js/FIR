from django.forms import ModelForm
from django import forms
from incidents.models import IncidentCategory, Incident, Comments, BusinessLine
from incidents.fields import DateTimeLocalField
from django.contrib.auth.forms import AuthenticationForm


class CustomAuthenticationForm(AuthenticationForm):
    username = forms.CharField(label=False,
                               max_length=30,
                               widget=forms.TextInput(attrs={'class': 'form-control',
                                                             'name': 'username',
                                                             'placeholder': 'Username'}))
    password = forms.CharField(label=False,
                               max_length=64,
                               widget=forms.PasswordInput(attrs={'class': 'form-control',
                                                                 'name': 'password',
                                                                 'placeholder': 'Password'}))
    remember = forms.BooleanField(required=False,
                                  label='Remember me',
                                  widget=forms.CheckboxInput(attrs={'class': 'checkbox',
                                                                    'name': 'remember'}))

class CustomAuthenticationTokenForm(ModelForm):
    def __init__(self, user, initial_device, **kwargs):
        super(CustomAuthenticationTokenForm, self).__init__(user, initial_device, **kwargs)
            

class IncidentForm(ModelForm):
    date = DateTimeLocalField()

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('for_user', None)
        permissions = kwargs.pop('permissions', None)
        has_permission = True
        if permissions is None:
            permissions = ['incidents.handle_incidents', ]
            has_permission = False
        super(ModelForm, self).__init__(*args, **kwargs)
        if self.user is not None:
            if not isinstance(permissions, (list, tuple)):
                permissions = [permissions, ]
            if 'instance' not in kwargs and not has_permission:
                permissions.append('incidents.report_events')
            self.fields['concerned_business_lines'].queryset = BusinessLine.authorization.for_user(self.user,
                                                                                                   permissions)
        self.fields['subject'].error_messages['required'] = 'This field is required.'
        self.fields['category'].error_messages['required'] = 'This field is required.'
        self.fields['concerned_business_lines'].error_messages['required'] = 'This field is required.'
        self.fields['detection'].error_messages['required'] = 'This field is required.'

        self.fields['severity'].error_messages['required'] = 'This field is required.'
        self.fields['is_major'].error_messages['required'] = 'This field is required.'

        self.fields['is_major'].label = 'Major?'

    def clean(self):
        cleaned_data = super(IncidentForm, self).clean()
        if self.user is not None:
            business_lines = cleaned_data.get("concerned_business_lines")
            is_incident = cleaned_data.get("is_incident")
            if not (business_lines or self.user.has_perm("incidents.handle_incidents")):
                self.add_error('concerned_business_lines',
                                forms.ValidationError("Incidents without business line can only be created by global incident handlers."))

        return cleaned_data

    class Meta:
        model = Incident
        exclude = ('opened_by', 'main_business_lines', 'is_starred', 'artifacts')


class CommentForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ModelForm, self).__init__(*args, **kwargs)
        self.fields['comment'].error_messages['required'] = 'This field is required.'
        self.fields['action'].error_messages['required'] = 'This field is required.'

    class Meta:
        model = Comments
        exclude = ('incident', 'opened_by')
        widgets = {
            'action': forms.Select(attrs={'required': True, 'class': 'form-control'})
        }


class UploadFileForm(forms.Form):
    title = forms.CharField()
    file = forms.FileField()
